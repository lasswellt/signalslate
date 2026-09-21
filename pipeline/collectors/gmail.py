"""
Gmail transport: list message ids for a window, with quota-aware retry.

Design decisions (docs/_research/2026-09-20_gmail-collector-and-agents.md §4, §5):
- The window goes to messages.list as epoch SECONDS. Date strings in `q` are read as midnight PST,
  so a UTC window expressed that way is silently shifted by hours.
- No batch, no history.list, no watch. Batch has the Graph envelope problem (outer 200, per-part
  failures) at an identical quota cost; history.list and push watches need a persisted cursor and
  a full-sync fallback. A timestamp read self-heals, those don't.
- No parallel calls. Gmail returns 429 for a per-user concurrent-request limit, so requests go
  strictly one at a time and lean on backoff instead of bursting.
- Messages are fetched format=full (20 units) and flattened in the collector (research §4, §7): the raw
  form is a nested base64url MIME tree with both a text/plain and a text/html copy of the body, and
  that decoding is Gmail-specific. Attachments are recorded as metadata only; attachments.get is never
  called.
- HTML bodies are untrusted input that an LLM will read. Hidden content (display:none, the hidden
  attribute, aria-hidden, script/style/head/title) is dropped because hidden text is a documented
  prompt-injection carrier that a human reading the same mail never sees.
"""
import base64
import binascii
import codecs
import random
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional, TypeVar, cast
from urllib.parse import quote

import requests

from pipeline import health
from pipeline.collectors import CollectionResult, Item

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 15
# Documented maximum for messages.list; anything smaller just costs more 5-unit calls.
PAGE_SIZE = 500
# 40 x 500 = 20k messages in one window. Past that something is wrong (a backfill after a long
# outage) and a truncated list would under-report exactly when it matters.
MAX_PAGES = 40
# Total requests per call. Backoff sleeps 1, 2, 4, 8, 16, 32s between them (~1 minute), which is
# the length of the per-user quota window that a rate-limit 403 is waiting out.
MAX_ATTEMPTS = 7
MAX_BACKOFF = 32
RETRY_STATUS = {429, 500, 502, 503, 504}
# Compared after lowercasing and dropping underscores, so RATE_LIMIT_EXCEEDED matches too.
RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}
# get costs 20 units against 6,000/min/user, so 300/min is the ceiling. 0.3s between sequential gets
# is ~200/min (call latency adds to it): headroom for the list calls and a retry burst.
GET_PACE_SECONDS = 0.3
# Characters of bodyText kept per message. A guess: re-tune from the dry run's real payload sizes.
BODY_CAP = 20000
# Characters of HTML parsed per message. BODY_CAP only slices the text AFTER parsing, so without this
# the parser's cost was set by the sender. A guess: re-tune from the dry run's real HTML sizes (legitimate
# newsletters are a few hundred KB at most, and only the first BODY_CAP characters of text are kept).
MAX_HTML_CHARS = 500000

# Indirections so tests neither wait nor depend on real randomness.
_sleep = time.sleep
_random = random.random


class GmailError(RuntimeError):
    """A Gmail call failed in a way the caller should report, not retry."""


def to_epoch_seconds(moment: datetime) -> int:
    """
    Naive-UTC datetime -> epoch seconds.

    The explicit tzinfo is the point: a naive .timestamp() reads the value as LOCAL time, which under
    any non-UTC container TZ shifts the window by the UTC offset.
    """
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _backoff(attempt: int) -> float:
    """Truncated exponential backoff: min(2^n + random fraction of a second, MAX_BACKOFF)."""
    return min(2**attempt + _random(), MAX_BACKOFF)


def _error_reasons(resp: requests.Response) -> set[str]:
    """
    Every reason-like string in a Gmail error body, normalised.

    Gmail documents errors[].reason, but Google's newer error shape puts the same information in
    error.status and error.details[].reason, so all three are read.
    """
    try:
        body = resp.json()
    except ValueError:
        return set()
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()

    found = [error.get("status")]
    for group in ("errors", "details"):
        entries = error.get(group)
        if isinstance(entries, list):
            found.extend(e.get("reason") for e in entries if isinstance(e, dict))
    return {str(r).lower().replace("_", "") for r in found if r}


def _retryable(resp: requests.Response) -> bool:
    if resp.status_code in RETRY_STATUS:
        return True
    # Rate limits arrive as 403, not only 429. Any OTHER 403 (insufficientPermissions, a revoked
    # scope) is permanent, and retrying it just burns a minute before failing anyway.
    return resp.status_code == 403 and bool(_error_reasons(resp) & RATE_REASONS)


def _call(token: str, path: str, params: Optional[dict] = None) -> dict:
    """GET one Gmail resource; `path` is relative to users/me, e.g. "/messages"."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GMAIL}{path}"

    last_status: Optional[int] = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise GmailError(f"{path}: {exc}") from exc

        if resp.status_code < 400:
            try:
                return resp.json()
            # RecursionError is not a ValueError: a pathologically deep body would otherwise escape
            # fetch_messages, which only catches GmailError, and discard the whole batch.
            except (ValueError, RecursionError) as exc:
                raise GmailError(f"{path}: non-JSON response") from exc

        if not _retryable(resp):
            raise GmailError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")
        last_status = resp.status_code
        if attempt < MAX_ATTEMPTS - 1:
            _sleep(_backoff(attempt))

    raise GmailError(f"{path}: still failing after {MAX_ATTEMPTS} attempts (last HTTP {last_status})")


def list_message_ids(token: str, since: datetime, until: datetime) -> list[dict]:
    """
    Every {id, threadId} Gmail lists for [since, until), all pages drained.

    The after:/before: boundary inclusivity and list ordering are undocumented, so callers overlap the
    window, window again on internalDate, and dedupe by id.
    """
    params = {
        "q": f"after:{to_epoch_seconds(since)} before:{to_epoch_seconds(until)}",
        "maxResults": PAGE_SIZE,
    }
    out: list[dict] = []
    page_token: Optional[str] = None

    for _ in range(MAX_PAGES):
        page_params = {**params, "pageToken": page_token} if page_token else dict(params)
        body = _call(token, "/messages", page_params)
        out.extend(body.get("messages") or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            return out

    raise GmailError(f"more than {MAX_PAGES} pages — window too large, results truncated")


# Void elements never get an end tag, so they must not push onto the open-element stack.
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# Their text is never shown to a reader (script/style are code, head/title are chrome).
_SKIP_TAGS = {"script", "style", "head", "title"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "ul", "ol", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", re.IGNORECASE)
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^\s\"';]+)", re.IGNORECASE)


def _is_hidden(attrs: list) -> bool:
    for name, value in attrs:
        if name == "hidden":  # boolean attribute: present means hidden, whatever the value
            return True
        if name == "aria-hidden" and (value or "").strip().lower() == "true":
            return True
        if name == "style" and _HIDDEN_STYLE.search(value or ""):
            return True
    return False


class _TextExtractor(HTMLParser):
    """
    HTML -> visible text.

    Open elements sit on a stack of (tag, hidden-including-ancestors) rather than a bare depth counter,
    because real mail leaves <p>/<li> unclosed: an end tag pops back to its nearest matching open tag,
    so a stray unclosed child cannot leave a hidden region open (dropping the rest of the mail) or
    close it early (leaking the hidden text).

    `_open` counts the tag names on the stack so an end tag with no open match is dropped in O(1). The
    scan it replaces made `<b>` * n + `</i>` * n quadratic (2.2s at n=10000, 20s at n=30000): every
    stray end tag walked the whole stack. A match still scans from the top, but popping stack[i:]
    removes what was scanned, so that work is amortized.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool]] = []
        self._open: Counter[str] = Counter()
        self._out: list[str] = []

    def _hidden(self) -> bool:
        return bool(self._stack) and self._stack[-1][1]

    def _newline(self) -> None:
        # Nested blocks (<ul><li>, <table><tr>) open and close back to back; one break is enough, and
        # only <br> runs may make a deliberate blank line.
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _VOID_TAGS:
            if tag == "br" and not self._hidden():
                self._out.append("\n")
            return
        hidden = self._hidden() or tag in _SKIP_TAGS or _is_hidden(attrs)
        self._stack.append((tag, hidden))
        self._open[tag] += 1
        if not hidden and tag in _BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        # HTMLParser's default calls start then end, so `<div style="display:none"/>SECRET</div>` closed
        # its own hidden region at once and leaked SECRET. Browsers ignore the "/" on non-void HTML
        # elements and keep the element open until its end tag, so mirror that: the model must see what
        # a human sees, including an unclosed hidden region hiding the rest of its parent.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if not self._open[tag]:
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                was_hidden = self._stack[i][1]
                for popped, _ in self._stack[i:]:
                    self._open[popped] -= 1
                del self._stack[i:]
                if not was_hidden and tag in _BLOCK_TAGS:
                    self._newline()
                return

    def handle_data(self, data: str) -> None:
        if self._hidden():
            return
        collapsed = re.sub(r"\s+", " ", data)
        # Source-formatting whitespace between tags is not content; keeping it would defeat _newline.
        if not collapsed.strip() and (not self._out or self._out[-1].endswith("\n")):
            return
        self._out.append(collapsed)

    def text(self) -> str:
        return "".join(self._out)


def _tidy(text: str) -> str:
    """Strip each line and collapse runs of blank lines to one."""
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html_to_text_capped(html: str) -> tuple[str, bool]:
    """
    HTML -> (visible text, whether the input was cut at MAX_HTML_CHARS).

    A cut can land inside a tag or a hidden region. Neither leaks: the parser drops a tag left
    unfinished at end of input, and an unclosed hidden element keeps hiding to the end.
    """
    cut = len(html) > MAX_HTML_CHARS
    parser = _TextExtractor()
    parser.feed(html[:MAX_HTML_CHARS])
    parser.close()
    return _tidy(parser.text()), cut


def _html_to_text(html: str) -> str:
    return _html_to_text_capped(html)[0]


_T = TypeVar("_T")


def _scrub(value: _T) -> _T:
    """
    A str with any lone surrogate (U+D800-U+DFFF) replaced by "?"; anything else is returned as is.

    A lone surrogate survives json.dumps (escaped) but makes the later json.dumps(ensure_ascii=False) +
    UTF-8 encode in the triage request raise UnicodeEncodeError. A Python str never holds a valid
    surrogate pair (that is one code point), so this cannot touch a legitimate emoji.
    """
    if isinstance(value, str):
        return cast(_T, value.encode("utf-8", "replace").decode("utf-8"))
    return value


# unicode_escape, raw_unicode_escape and utf-7 turn ASCII text such as "\ud800" or "+2AA-" into a lone
# surrogate, and idna/punycode/undefined are not charsets. codecs.lookup(...)._is_text_encoding is True for
# all of them (measured), so it only filters the bytes-to-bytes and str-to-str codecs (base64, hex, zlib,
# rot13...); this denylist, keyed on the canonical codec name, is what closes the rest.
_NON_CHARSET_CODECS = frozenset(
    {"unicode_escape", "raw_unicode_escape", "utf_7", "idna", "punycode", "undefined", "rot_13", "base64", "hex", "zlib", "bz2", "uu", "quopri"}
)


def _text_charset(name: str) -> Optional[str]:
    """The canonical codec name when `name` is a real text charset, else None. `name` is sender-controlled."""
    try:
        info = codecs.lookup(name)
    except (LookupError, ValueError):
        return None
    # _is_text_encoding is private: if a future Python drops it, treat the codec as not text (fail closed).
    if not getattr(info, "_is_text_encoding", False):
        return None
    if info.name.lower().replace("-", "_") in _NON_CHARSET_CODECS:
        return None
    return info.name


def _headers(raw: object) -> dict[str, str]:
    """Header list -> {lowercased name: whitespace-collapsed value}. Gmail preserves the sender's name casing."""
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for header in raw:
        if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
            out.setdefault(header["name"].lower(), _scrub(" ".join(header["value"].split())))
    return out


def _decode_part(part: dict) -> Optional[str]:
    """
    Decode one leaf part's body, or None when there is nothing usable.

    Gmail's base64url omits padding, which urlsafe_b64decode rejects, so it is restored first. A part
    whose data is missing (a large body Gmail only serves through attachments.get) or is corrupt yields
    None: one bad part must not fail the whole message.
    """
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    match = _CHARSET.search(_headers(part.get("headers")).get("content-type", ""))
    charset = (_text_charset(match.group(1)) if match else None) or "utf-8"
    try:
        return _scrub(raw.decode(charset, errors="replace"))
    except UnicodeError:
        # A text codec can still reject errors="replace" input it cannot process at all.
        return _scrub(raw.decode("utf-8", errors="replace"))


def _walk(part: dict, plain: list[str], html: list[str], attachments: list[dict]) -> None:
    """Collect body text and attachment metadata from the MIME tree, depth first, in document order."""
    body = part.get("body") or {}
    if part.get("filename"):
        # A named part is an attachment even when its type is text/*: it is not the message body.
        size = body.get("size")
        attachments.append(
            {
                "filename": _scrub(part["filename"]),
                "mimeType": _scrub(part.get("mimeType")),
                "size": size if isinstance(size, int) else 0,
            }
        )
        return

    children = part.get("parts")
    if isinstance(children, list) and children:
        for child in children:
            if isinstance(child, dict):
                _walk(child, plain, html, attachments)
        return

    mime = (part.get("mimeType") or "").lower()
    if mime in ("text/plain", "text/html"):
        text = _decode_part(part)
        if text is not None:
            (plain if mime == "text/plain" else html).append(text)


def flatten_message(msg: dict) -> dict:
    """
    A messages.get format=full response -> the compact payload stored on the Item.

    `subject` stays top-level so pipeline.collect's PREVIEW_FIELDS shows it. `internalDate` keeps the
    API's own string; parse_internal_date() turns it into the Item's occurred_at. Absent headers are
    None, not "", so "no Cc" and "empty Cc" stay distinguishable downstream.
    """
    payload = msg.get("payload") or {}
    headers = _headers(payload.get("headers"))
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []
    _walk(payload, plain, html, attachments)

    # text/plain wins when it has any content; an HTML-only or whitespace-only-plain mail falls back.
    text = _tidy("\n".join(plain))
    html_cut = False
    if not text:
        text, html_cut = _html_to_text_capped("\n".join(html))

    return {
        "subject": headers.get("subject"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "date": headers.get("date"),
        "messageId": headers.get("message-id"),
        "inReplyTo": headers.get("in-reply-to"),
        "listUnsubscribe": headers.get("list-unsubscribe"),
        "threadId": _scrub(msg.get("threadId")),
        "labelIds": [_scrub(label) for label in msg.get("labelIds") or []],
        "snippet": _scrub(msg.get("snippet") or ""),
        "internalDate": _scrub(msg.get("internalDate")),
        "bodyText": text[:BODY_CAP],
        "bodyTruncated": html_cut or len(text) > BODY_CAP,
        "attachments": attachments,
    }


def parse_internal_date(value: object) -> Optional[datetime]:
    """
    internalDate (milliseconds since epoch, as a string) -> naive UTC, or None when unparseable.

    Not parse_iso: internalDate is not ISO. Arithmetic from the epoch keeps it naive UTC with no
    dependence on the machine's timezone.
    """
    try:
        return datetime(1970, 1, 1) + timedelta(milliseconds=int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_message(token: str, message_id: str) -> dict:
    """
    GET one message at format=full.

    Raises GmailError on failure. The id is quoted so a hostile value cannot rewrite the request path.
    """
    return _call(token, f"/messages/{quote(message_id, safe='')}", {"format": "full"})


def fetch_messages(token: str, message_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Fetch each id sequentially, paced by GET_PACE_SECONDS.

    Returns (messages, failures). One unfetchable message (deleted between list and get, say) is a
    failure entry {"id", "error"}, not an abort: the caller decides what a partial batch means.
    """
    messages: list[dict] = []
    failures: list[dict] = []
    for i, message_id in enumerate(message_ids):
        if i:
            _sleep(GET_PACE_SECONDS)
        try:
            messages.append(fetch_message(token, message_id))
        except GmailError as exc:
            failures.append({"id": message_id, "error": str(exc)})
    return messages, failures


def collect_gmail(label: str, refresh_token: Optional[str], since: datetime, until: datetime) -> CollectionResult:
    """
    Mail for [since, until) from one Gmail account. Never raises for an expected failure.

    `refresh_token` is only checked for presence: the exchange itself reads .env through
    health.gmail_token_response, so the health check and the collector cannot disagree about config.
    """
    source = f"gmail_{label}"
    if not refresh_token:
        return CollectionResult(source, "error", f"No token in .env for {label}")

    # Looked up on the module, not imported by name, so there is one token path to stub or change.
    try:
        access_token = health.gmail_token_response(label)["access_token"]
    except health.GmailAuthError as exc:
        return CollectionResult(source, "error", str(exc))
    except RuntimeError as exc:
        return CollectionResult(source, "error", str(exc))
    except requests.RequestException as exc:
        return CollectionResult(source, "error", f"token refresh failed: {exc}")
    except (KeyError, TypeError):
        return CollectionResult(source, "error", "token response had no access_token")

    try:
        listed = list_message_ids(access_token, since, until)
    except GmailError as exc:
        return CollectionResult(source, "error", str(exc))

    # Overlapping windows and undocumented list semantics can repeat an id; fetching it twice costs
    # 20 units for nothing. dict.fromkeys keeps the list order.
    ids = list(dict.fromkeys(m["id"] for m in listed if isinstance(m, dict) and isinstance(m.get("id"), str)))
    messages, failures = fetch_messages(access_token, ids)

    items: list[Item] = []
    unplaceable = 0
    for msg in messages:
        occurred = parse_internal_date(msg.get("internalDate"))
        message_id = msg.get("id")
        if occurred is None or not isinstance(message_id, str):
            unplaceable += 1  # cannot be windowed, so it cannot be kept; not a fetch failure
            continue
        # after:/before: boundary inclusivity is undocumented, so the window is enforced here.
        if not since <= occurred < until:
            continue
        try:
            payload = flatten_message(msg)
        except Exception as exc:  # noqa: BLE001 - mail is untrusted; one hostile message must not sink the batch
            # Class name only: the exception text can echo message content. Counted as a failure, not
            # skipped, so the run is "partial" and the window is retried rather than silently advanced.
            failures.append({"id": message_id, "error": f"{type(exc).__name__}: could not parse message"})
            continue
        items.append(Item("mail", message_id, occurred, payload))

    detail = f"{len(items)} messages"
    if unplaceable:
        detail += f", {unplaceable} skipped (no usable internalDate)"

    if failures:
        summary = f"{len(failures)} failed: {failures[0]['id']}: {failures[0]['error']}"
        # Every fetch failing means the token or the API is broken, not one bad message.
        if len(failures) == len(ids):
            return CollectionResult(source, "error", f"{detail} ({summary})")
        return CollectionResult(source, "partial", f"{detail} ({summary})", items)
    return CollectionResult(source, "ok", detail, items)
