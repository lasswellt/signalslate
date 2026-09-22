"""
Normalized item: one source-agnostic shape for everything the digest step reads.

Design decisions (docs/_research/2026-09-20_gmail-collector-and-agents.md §8, "Normalized item"):
- The schema is a PROPOSAL, not a published standard. Adapters are pure functions over the stored
  payload (no I/O, no clock), so each one is fixture-testable and a re-run gives identical output.
- Adapters read what the collector already flattened (pipeline.collectors.gmail.flatten_message).
  They never re-parse MIME, so hidden-HTML stripping happens exactly once, at collection.
- Everything here comes from third parties, so `trust` is pinned to "untrusted_third_party" and a
  downstream prompt must treat body_text as data, never as instructions.
- Body text is capped at NORMALIZED_BODY_CAP after quote/signature stripping (§8 Q3: ~1.5-2k chars).
  The strip is a deterministic heuristic, not a parser: it is deliberately conservative and prefers
  leaving a quote in over dropping real text.
- permalink stays None: a Gmail URL format that works across accounts is unverified, so it is
  deferred to the render phase.
"""
import html
import json
import re
from datetime import datetime
from email.utils import getaddresses
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

# Research §8 Q3: ~1.5-2k chars is enough for triage once quotes and signatures are gone.
NORMALIZED_BODY_CAP = 2000
NO_SUBJECT = "(no subject)"
# A real signature is a few lines; a "--" line followed by more than this is a separator inside the
# message, and dropping everything after it would delete real text.
MAX_SIGNATURE_LINES = 15


class NoAdapterError(ValueError):
    """No normalizer exists for this source yet."""


class PayloadError(ValueError):
    """The stored payload is not a JSON object."""


class Participant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    address: str
    role: Literal["from", "to", "cc", "host", "attendee"]


class Attachment(BaseModel):
    """Metadata only: the collector never downloads attachment content."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    mime_type: Optional[str] = None
    size: int = 0


class NormalizedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # "<source>:<external_id>"
    source: str  # full source id, e.g. gmail_personal
    kind: str  # mirrors the collected item_type
    occurred_at: datetime  # naive UTC
    title: str
    body_text: str
    body_truncated: bool
    participants: list[Participant]
    thread_key: Optional[str] = None
    labels: list[str]
    direction: Literal["inbound", "outbound"]
    is_unread: bool
    # A List-Unsubscribe header is present: a cheap "bulk/newsletter" triage signal.
    is_bulk: bool
    attachments: list[Attachment]
    trust: Literal["untrusted_third_party"] = "untrusted_third_party"
    raw_ref: str
    permalink: Optional[str] = None


_ATTRIBUTION = re.compile(r"^on\s.+\swrote:$", re.IGNORECASE)
_ON_PREFIX = re.compile(r"^on\s", re.IGNORECASE)
_SIGNATURE_DELIMITER = re.compile(r"^-- ?$")


def _attribution_index(lines: list[str]) -> Optional[int]:
    """Index of the first reply-attribution line ("On <date>, <who> wrote:"), or None."""
    for i, line in enumerate(lines):
        text = line.strip()
        if _ATTRIBUTION.match(text):
            return i
        # Mail clients wrap a long attribution onto a second line.
        if _ON_PREFIX.match(text) and i + 1 < len(lines) and _ATTRIBUTION.match(f"{text} {lines[i + 1].strip()}"):
            return i
    return None


def strip_quoted_reply(text: str) -> str:
    """
    Drop quoted reply lines, the attribution line and everything after it, and a trailing signature.

    Deterministic and conservative. Text above the attribution is always kept, and a "-- " delimiter
    with nothing above it, or with an implausibly long tail, is left alone so real text is never lost.
    """
    lines = [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if not line.startswith(">")]

    cut = _attribution_index(lines)
    if cut is not None:
        lines = lines[:cut]

    delimiters = [i for i, line in enumerate(lines) if _SIGNATURE_DELIMITER.match(line)]
    if delimiters:
        last = delimiters[-1]
        if len(lines) - last - 1 <= MAX_SIGNATURE_LINES and any(line.strip() for line in lines[:last]):
            lines = lines[:last]

    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in lines)).strip()


def truncate(text: str, cap: int) -> tuple[str, bool]:
    """
    Cut `text` to at most `cap` chars at a whitespace boundary. Returns (text, was_truncated).

    Nothing is appended: the flag carries the signal, and an ellipsis would count against the cap.
    A single token longer than the cap has no boundary to cut at, so it is hard-cut.
    """
    if len(text) <= cap:
        return text, False
    window = text[:cap]
    if not text[cap].isspace():
        boundary = re.search(r"\s\S*$", window)
        if boundary:
            window = window[: boundary.start()]
    return window.rstrip(), True


def _participants(payload: dict) -> list[Participant]:
    out: list[Participant] = []
    fields: tuple[tuple[str, Literal["from", "to", "cc"]], ...] = (("from", "from"), ("to", "to"), ("cc", "cc"))
    for field, role in fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            continue
        for name, address in getaddresses([value]):
            if address:
                out.append(Participant(name=name, address=address, role=role))
    return out


def normalize_gmail(source: str, external_id: str, occurred_at: datetime, payload: dict) -> NormalizedItem:
    """
    A flatten_message() payload -> NormalizedItem.

    Gmail's snippet arrives HTML-entity-escaped (&#39;, &amp;), so it is unescaped before use as the
    body fallback for a message whose bodyText came out empty.
    """
    body = payload.get("bodyText") or ""
    if not body.strip():
        body = html.unescape(payload.get("snippet") or "")
    body_text, cut = truncate(strip_quoted_reply(body), NORMALIZED_BODY_CAP)

    labels = list(payload.get("labelIds") or [])
    thread_id = payload.get("threadId")

    return NormalizedItem(
        id=f"{source}:{external_id}",
        source=source,
        kind="mail",
        occurred_at=occurred_at,
        title=(payload.get("subject") or "").strip() or NO_SUBJECT,
        body_text=body_text,
        body_truncated=bool(payload.get("bodyTruncated")) or cut,
        participants=_participants(payload),
        thread_key=f"gmail:{thread_id}" if thread_id else None,
        labels=labels,
        direction="outbound" if "SENT" in labels else "inbound",
        is_unread="UNREAD" in labels,
        is_bulk=bool(payload.get("listUnsubscribe")),
        attachments=[
            Attachment(filename=a["filename"], mime_type=a.get("mimeType"), size=a.get("size") or 0)
            for a in payload.get("attachments") or []
            if isinstance(a, dict) and isinstance(a.get("filename"), str)
        ],
        raw_ref=external_id,
    )


NO_TOPIC = "Zoom meeting"
NOT_HOST_SUFFIX = " (you did not host this meeting)"


def normalize_zoom(source: str, external_id: str, occurred_at: datetime, payload: dict) -> NormalizedItem:
    """
    A pipeline.collectors.zoom Item payload -> NormalizedItem.

    Zoom items have no "from" participant (a meeting has a host, not a sender), so
    pipeline.triage's stub fallback and prompt-building both cope with that already: `stub_record`
    treats a missing "from" as an item with no sender label, and `_participant_line` just prefixes
    "host"/"attendee" the same way it prefixes "from"/"to"/"cc".
    """
    summary_available = bool(payload.get("summary_available"))
    if summary_available:
        body = payload.get("summary_content") or ""
    else:
        body = "No AI Companion summary available."
        if payload.get("summary_unavailable_reason") == "not_host":
            body += NOT_HOST_SUFFIX

    transcript_text = payload.get("transcript_text")
    if transcript_text:
        body += "\n\nTranscript excerpt:\n" + transcript_text

    body_text, cut = truncate(body, NORMALIZED_BODY_CAP)
    body_truncated = cut or bool(payload.get("transcript_truncated"))

    host_email = payload.get("host_email") or None
    participants: list[Participant] = []
    if host_email:
        participants.append(Participant(name="", address=host_email, role="host"))
    for attendee in payload.get("participants") or []:
        if not isinstance(attendee, dict):
            continue
        email = attendee.get("email")
        # Participant.address is required (not Optional): an attendee row with no email has
        # nothing usable for it, so it is dropped rather than stored with a blank address. The
        # host is deduped the same way an entry may legitimately list them as an attendee too.
        if not email or email == host_email:
            continue
        participants.append(Participant(name=attendee.get("name") or "", address=email, role="attendee"))

    hosted = bool(payload.get("hosted"))
    labels = ["zoom"]
    if not summary_available:
        labels.append("no-summary")
    labels.append("hosted" if hosted else "attended")
    if payload.get("transcript_available"):
        labels.append("transcript")

    return NormalizedItem(
        id=f"{source}:{external_id}",
        source=source,
        kind="meeting",
        occurred_at=occurred_at,
        title=(payload.get("topic") or "").strip() or NO_TOPIC,
        body_text=body_text,
        body_truncated=body_truncated,
        participants=participants,
        thread_key=None,
        labels=labels,
        direction="inbound",
        is_unread=False,
        is_bulk=False,
        attachments=[],
        raw_ref=external_id,
        permalink=payload.get("summary_doc_url") or None,
    )


def normalize(source: str, item_type: str, external_id: str, occurred_at: datetime, payload: object) -> NormalizedItem:
    """
    Dispatch on the source prefix (gmail_*) or exact source ("zoom") to that source's adapter.

    `payload` is a dict (collectors' Item) or a JSON string (pipeline.db.CollectedItem stores it that way).
    Raises NoAdapterError for a source with no adapter, PayloadError for a payload that is not a JSON object.
    """
    if not source.startswith("gmail_") and source != "zoom":
        raise NoAdapterError(f"no normalizer for source {source!r}")

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise PayloadError(f"{source}:{external_id}: payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PayloadError(f"{source}:{external_id}: payload is not a JSON object")

    item = normalize_zoom(source, external_id, occurred_at, payload) if source == "zoom" else normalize_gmail(
        source, external_id, occurred_at, payload
    )
    # kind follows the stored item_type so it cannot disagree with what the collector recorded.
    return item.model_copy(update={"kind": item_type})
