"""
Link and text harvester for shipment emails.

Design decisions:

- Uses the stdlib html.parser (no new dependency) and keeps a[href], which the Gmail connector's
  text extractor drops: many merchants (swiship, Narvar, FedEx, UPS) put the tracking number only
  in a link target, never in visible text.
- Wrapper URLs (awstrack.me /L0/<encoded>, Mandrill p=<base64url JSON>, generic ?url=/?u=/
  ?redirect=/?target=) are decoded locally, with percent-decoding up to 2 levels. Redirects are
  NEVER followed: this module imports no HTTP client and opens no sockets, so harvesting an email
  cannot leak that it was read or trigger a tracking pixel. Opaque tokens (w-mt.co, upn=) are left
  as-is.
- Visible text turns block elements, <br> and table cells into newlines/spaces so adjacent cells
  never glue into one token ("Tracking #:<td>154..</td><td>Track" must not become "154...Track").
  script, style, head and hidden elements are removed.
- id= is a tracking number ONLY on swiship.com and amazon progress-tracker hosts; on any other
  host it is an order/session id and is ignored.
- Email bodies are never logged or persisted; only extracted tokens leave this module.
"""
import base64
import json
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

_MAX_DEPTH = 3
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")
_PARAM_RE = re.compile(
    r"(?:tracknum|tracknums|tracking_numbers?|trackingnumber|trknbr|tlabels|nums|"
    r"tracking-id|trackingid|awb)=([A-Za-z0-9,%]{8,120})", re.I)
_ID_HOST_RE = re.compile(
    r"(?:swiship\.com|amazon\.[a-z.]+/progress-tracker)[^\s]*?[?&]id=([A-Za-z0-9]{10,20})", re.I)
_PATH_CARRIER_RE = re.compile(r"/(usps|ups|fedex|dhl)/([A-Za-z0-9]{10,34})(?=[/?#]|$)", re.I)
_PATH_TRACKING_RE = re.compile(
    r"/tracking/[^/?#]+/(usps|ups|fedex|dhl)/([A-Za-z0-9]{10,34})(?=[/?#]|$)", re.I)
_NARVAR_SEG_RE = re.compile(r"/(UPSN|FEDEX|USPS)(?=[/?#]|$)")
_WRAPPER_PARAMS = ("url", "u", "redirect", "target")
_NARVAR_CARRIERS = {"UPSN": "ups", "FEDEX": "fedex", "USPS": "usps"}
_HOST_CARRIERS = (("ups.com", "ups"), ("fedex.com", "fedex"), ("usps.com", "usps"),
                  ("dhl.com", "dhl"), ("swiship.com", "amazon"), ("amazon.", "amazon"))
_PATH_HINTS = {"ups": "ups", "fedex": "fedex", "usps": "usps", "dhl": "dhl"}
_UPPER_PREFIXES = ("1z", "tb", "yt")

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "hr", "pre"})
_CELL_TAGS = frozenset({"td", "th"})
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
_VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input", "wbr", "area", "base", "col"})
_HIDDEN_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px)?\s*(?:;|$)",
                        re.I)


@dataclass(frozen=True)
class UrlCandidate:
    """A tracking-number token found in a URL.

    Attributes:
        token: Candidate tracking number (not yet validated).
        carrier_hint: Carrier slug inferred from the URL, or None.
        source_url_host: Lowercase host of the URL the token came from.
    """
    token: str
    carrier_hint: Optional[str]
    source_url_host: str


@dataclass(frozen=True)
class Harvest:
    """Result of harvesting one message.

    Attributes:
        text: Visible text with word separators preserved.
        links: Every href plus locally decoded wrapper targets, de-duplicated, in order.
        url_candidates: Tracking-number tokens extracted from the links.
    """
    text: str
    links: tuple[str, ...]
    url_candidates: tuple[UrlCandidate, ...]


class _Extractor(HTMLParser):
    """Collects visible text and a[href] values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hrefs: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"].strip())
        if tag in _VOID_TAGS:
            if tag in _BLOCK_TAGS:
                self.parts.append("\n")
            return
        hidden = (tag in _SKIP_TAGS or "hidden" in a or a.get("aria-hidden") == "true"
                  or bool(_HIDDEN_RE.search(a.get("style", ""))))
        self._stack.append((tag, hidden))
        if hidden:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, hidden in self._stack[i:]:
                    if hidden:
                        self._skip -= 1
                del self._stack[i:]
                break
        else:
            return
        if self._skip == 0:
            self.parts.append("\n" if tag in _BLOCK_TAGS else " " if tag in _CELL_TAGS else "")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _unquote_levels(s: str) -> set[str]:
    """Return s plus its 1- and 2-level percent-decodings."""
    one = urllib.parse.unquote(s)
    return {s, one, urllib.parse.unquote(one)}


def _decode_wrappers(url: str, depth: int = 0) -> list[str]:
    """Return locally decodable target URLs embedded in a wrapper URL (never fetched).

    Args:
        url: A URL from an email.
        depth: Current recursion depth (bounded by _MAX_DEPTH).

    Returns:
        Decoded http(s) targets, possibly empty.
    """
    if depth >= _MAX_DEPTH:
        return []
    found: list[str] = []
    m = re.search(r"awstrack\.me/L0/(.+)", url, re.I)
    if m:
        for v in _unquote_levels(m.group(1)):
            if v.lower().startswith(("http://", "https://")):
                found.append(re.sub(r"/\d+/[^/]*$", "", v))
                break
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    for name in _WRAPPER_PARAMS:
        for v in query.get(name, []):
            for cand in _unquote_levels(v):
                if cand.lower().startswith(("http://", "https://")):
                    found.append(cand)
                    break
    if re.search(r"mandrillapp\.com|/track/click", url, re.I):
        for p in query.get("p", []):
            try:
                raw = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode("utf8", "ignore")
                inner = json.loads(raw)
            except (ValueError, TypeError):
                continue
            target = inner.get("url") if isinstance(inner, dict) else None
            if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
                found.append(urllib.parse.unquote(target))
    out: list[str] = []
    for f in found:
        out.append(f)
        out.extend(_decode_wrappers(f, depth + 1))
    return out


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _carrier_for(url: str) -> Optional[str]:
    """Infer a carrier slug from host, narvar path segment or goshippo-style path."""
    host = _host(url)
    path = urllib.parse.urlsplit(url).path
    if host.endswith("narvar.com"):
        seg = _NARVAR_SEG_RE.search(path)
        if seg:
            return _NARVAR_CARRIERS[seg.group(1)]
    for suffix, carrier in _HOST_CARRIERS:
        if host == suffix or host.endswith("." + suffix) or (suffix.endswith(".") and suffix in host):
            return carrier
    seg = re.match(r"/(usps|ups|fedex|dhl)/", path, re.I)
    return _PATH_HINTS[seg.group(1).lower()] if seg else None


def _norm(tok: str) -> str:
    return tok.upper() if tok[:2].lower() in _UPPER_PREFIXES else tok


def _candidates(url: str) -> list[UrlCandidate]:
    """Extract tracking-number tokens from one (already decoded) URL."""
    host = _host(url)
    hint = _carrier_for(url)
    out: list[UrlCandidate] = []
    for v in _PARAM_RE.findall(url):
        for tok in urllib.parse.unquote(v).split(","):
            tok = tok.strip()
            if len(tok) >= 8:
                out.append(UrlCandidate(_norm(tok), hint, host))
    for v in _ID_HOST_RE.findall(url):
        out.append(UrlCandidate(v, hint, host))
    for rx in (_PATH_TRACKING_RE, _PATH_CARRIER_RE):
        for car, num in rx.findall(url):
            out.append(UrlCandidate(_norm(num), _PATH_HINTS[car.lower()], host))
    return out


def harvest(html: Optional[str], plain: Optional[str]) -> Harvest:
    """Harvest visible text, links and URL tracking candidates from a message.

    Never fetches anything; wrapper URLs are decoded locally only.

    Args:
        html: HTML body, preferred when non-empty.
        plain: Plain-text body (including markdown "[](https://...)" link form), used when
            html is absent.

    Returns:
        Harvest with text, links and url_candidates (all empty for empty input).
    """
    hrefs: list[str] = []
    if html:
        ex = _Extractor()
        ex.feed(html)
        ex.close()
        text = "".join(ex.parts)
        hrefs = ex.hrefs
        # bare URLs shown as text also count as links
        hrefs += _URL_RE.findall(text)
    else:
        text = plain or ""
        hrefs = _URL_RE.findall(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    links: list[str] = []
    seen: set[str] = set()
    for h in hrefs:
        for u in [h, *_decode_wrappers(h)]:
            if u.lower().startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                links.append(u)
    cands: list[UrlCandidate] = []
    cseen: set[UrlCandidate] = set()
    for u in links:
        for c in _candidates(u):
            if c not in cseen:
                cseen.add(c)
                cands.append(c)
    return Harvest(text=text, links=tuple(links), url_candidates=tuple(cands))
