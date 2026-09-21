"""
Triage: the schema the model must fill, plus the deterministic checks around its output.

Design decisions (docs/_research/2026-09-20_gmail-collector-and-agents.md §8, Q1, Q3, Q4):
- Model output is untrusted input. Nothing the model returns reaches the digest without passing
  validate_batch(), which drops unknown ids, keeps the first of duplicates and sanitizes every string.
- The schema is deliberately flat and constraint-free: structured outputs reject numeric min/max and
  string length limits, so categories and importance are string enums and every range or length is
  enforced here, in Python, after parsing. Every field is required (Optional ones are nullable, not
  defaulted) so the generated JSON schema stays trivial.
- The model only ever sees per-run aliases (m001, m002, ...). It never sees or echoes a real item id,
  so it cannot mis-attribute a record or invent one that collides with a stored item.
- The model never emits URLs. sanitize_text() strips any it tries anyway; links come later from our own
  id -> permalink map.
- A failed model call degrades to stub_record(): a partial digest, never a dead run.
- Pure logic: no SDK, no I/O, no clock, no randomness. The request builder and the API call are added
  in a later task, in their own section below.
"""
import datetime
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Optional, TypeVar

from pydantic import BaseModel, ConfigDict

from pipeline.normalize import NO_SUBJECT, NormalizedItem, truncate

# A guess, not a measurement: small enough that one bad batch costs little, large enough to keep the
# call count low. Revisit once real runs show output quality and token use per batch.
BATCH_SIZE = 20

ONE_LINE_MAX = 200
ACTION_TEXT_MAX = 300
PERSON_MAX = 80
PEOPLE_MAX = 10

T = TypeVar("T")


# --- Schema (model-facing) -------------------------------------------------------------------------


class Category(str, Enum):
    ACTION = "action"
    FYI = "fyi"
    MEETING = "meeting"
    PERSONAL = "personal"
    NEWSLETTER = "newsletter"
    NOTIFICATION = "notification"
    OTHER = "other"


class Importance(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TriageRecord(BaseModel):
    """One triaged message. `id` is the per-run alias, never a real item id."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    importance: Importance
    one_line: str
    action_needed: bool
    action_text: Optional[str]
    # ISO date (YYYY-MM-DD) when the message states a deadline, else null.
    due: Optional[str]
    people: list[str]


class TriageBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[TriageRecord]


# --- Downstream shape (not model-facing) -----------------------------------------------------------


class TriagedItem(BaseModel):
    """What the digest consumes: a record tied back to the REAL item id."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    alias: str
    record: TriageRecord
    # True when the record came from stub_record() because the model call failed or omitted the item.
    stubbed: bool


# --- Aliasing and batching -------------------------------------------------------------------------


def assign_aliases(
    items: Sequence[NormalizedItem],
) -> tuple[list[tuple[str, NormalizedItem]], dict[str, str]]:
    """
    Give each item a per-call alias (m001, m002, ...) in input order.

    Returns (alias, item) pairs plus an alias -> real item id map. Aliases are zero-padded to three
    digits and simply widen past 999, so they stay unique and sort-stable within one call. They are
    not global: the same item gets a different alias in a different run.
    """
    pairs = [(f"m{n:03d}", item) for n, item in enumerate(items, start=1)]
    return pairs, {alias: item.id for alias, item in pairs}


def batches(seq: Sequence[T], size: int = BATCH_SIZE) -> Iterator[list[T]]:
    """
    Yield consecutive lists of at most `size` items, in order. Empty input yields nothing.

    Raises ValueError for size < 1. It is checked eagerly, not on first iteration, so a bad size fails
    at the call site instead of inside a later loop.
    """
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    return (list(seq[start : start + size]) for start in range(0, len(seq), size))


# --- Sanitizing model output -----------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_HTML = re.compile(r"<!--.*?-->|</?[A-Za-z][^>]*>", re.DOTALL)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def sanitize_text(text: str, max_len: int) -> str:
    """
    Strip links, markup and control characters, collapse whitespace, cap at `max_len`.

    Control characters and tags become a space rather than nothing so that "ht<b></b>tps://x" cannot
    reassemble into a URL after stripping. Markdown links keep only their label. The cut is at a
    whitespace boundary when there is one.
    """
    text = _CONTROL.sub(" ", text)
    text = _HTML.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return truncate(text, max_len)[0]


def _valid_due(due: Optional[str]) -> Optional[str]:
    """Only a real calendar date in YYYY-MM-DD form survives; fromisoformat alone also accepts other layouts."""
    if due is None:
        return None
    candidate = due.strip()
    if not _ISO_DATE.fullmatch(candidate):
        return None
    try:
        return datetime.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def sanitize_record(record: TriageRecord) -> TriageRecord:
    """
    Clean every free-text field of a model-produced record. Never raises on model garbage.

    action_needed is left as the model said even when action_text ends up empty: guessing which of the
    two was wrong would be inventing data.
    """
    people: list[str] = []
    for person in record.people:
        cleaned = sanitize_text(person, PERSON_MAX)
        if cleaned and cleaned not in people:
            people.append(cleaned)

    action_text = sanitize_text(record.action_text, ACTION_TEXT_MAX) if record.action_text is not None else ""

    return record.model_copy(
        update={
            "one_line": sanitize_text(record.one_line, ONE_LINE_MAX),
            "action_text": action_text or None,
            "due": _valid_due(record.due),
            "people": people[:PEOPLE_MAX],
        }
    )


# --- Validating a batch of model output ------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    valid: list[TriageRecord]  # sanitized, in sent-alias order
    missing: list[str]  # sent aliases the output omitted: the caller retries these once
    unknown: list[str]  # returned aliases that were never sent (dropped, never trusted)
    duplicates: list[str]  # sent aliases returned more than once (first record kept)


def validate_batch(records: Sequence[TriageRecord], sent_aliases: Sequence[str]) -> ValidationResult:
    """
    Reconcile the model's records against the aliases that were sent.

    Unknown aliases are dropped, duplicates keep the FIRST record, and each kept record is sanitized.
    Output order follows `sent_aliases`, not the model's order, so a shuffled response still lines up.
    """
    sent = list(dict.fromkeys(sent_aliases))
    sent_set = set(sent)

    kept: dict[str, TriageRecord] = {}
    unknown: list[str] = []
    duplicates: list[str] = []
    for record in records:
        if record.id not in sent_set:
            if record.id not in unknown:
                unknown.append(record.id)
        elif record.id in kept:
            if record.id not in duplicates:
                duplicates.append(record.id)
        else:
            kept[record.id] = record

    return ValidationResult(
        valid=[sanitize_record(kept[alias]) for alias in sent if alias in kept],
        missing=[alias for alias in sent if alias not in kept],
        unknown=unknown,
        duplicates=duplicates,
    )


# --- Deterministic fallback ------------------------------------------------------------------------


def stub_record(alias: str, item: NormalizedItem) -> TriageRecord:
    """
    Deterministic record from the subject and sender, for when the model call failed or skipped `item`.

    Accepts any NormalizedItem. A title that is empty (or nothing but a link) falls back to NO_SUBJECT
    so the digest line is never blank. The sender suffix shares the ONE_LINE_MAX budget, so a very long
    title can push it out: the title is the more useful half.
    """
    sender = next((p for p in item.participants if p.role == "from"), None)
    sender_label = ((sender.name or "").strip() or sender.address) if sender else ""
    person = sanitize_text(sender_label, PERSON_MAX)

    title = sanitize_text(item.title, ONE_LINE_MAX) or NO_SUBJECT
    line = f"{title} (from {person})" if person else title

    return TriageRecord(
        id=alias,
        category=Category.OTHER,
        importance=Importance.LOW if item.is_bulk else Importance.NORMAL,
        one_line=sanitize_text(line, ONE_LINE_MAX),
        action_needed=False,
        action_text=None,
        due=None,
        people=[person] if person else [],
    )
