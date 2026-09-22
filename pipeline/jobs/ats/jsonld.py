"""
schema.org JobPosting (JSON-LD) fallback adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Structurally different from every other adapter in this package: there is no ATS-hosted API
  host to guess a slug against. board_id here is the careers-page URL itself, since resolve.py's
  ladder (research doc: URL pattern -> slug probe -> HTML fingerprint -> Claude web research ->
  JSON-LD) is expected to hand this adapter whatever URL it fingerprinted as HTML-confirmed, not a
  short slug.
- match_url() always returns None: unlike Greenhouse/Lever/etc., an arbitrary company careers page
  has no distinctive URL shape to pattern-match on. This adapter is only ever reached via the
  resolver ladder's HTML-fingerprint step, never via match_any_url()'s URL-pattern pass.
- The response here is HTML, not JSON, so this cannot route through pipeline.jobs.ats.fetch_json()
  (JSON-only). _fetch_text() is a module-local mirror of fetch_json()'s retry/backoff/Retry-After
  structure, returning the raw response body, matching workday.py's/personio.py's precedent of a
  module-local helper for a request shape pipeline.jobs.ats.fetch_json() doesn't cover.
- No BeautifulSoup/lxml/extruct dependency: this project has none, and the research doc flags
  extruct as an unverified library claim. _JsonLdExtractor is a stdlib html.parser.HTMLParser
  subclass that collects the raw text of every <script type="application/ld+json"> block, the same
  approach pipeline/collectors/gmail.py's _TextExtractor takes for HTML text extraction. HTMLParser
  treats script content as CDATA per the HTML5 content model, so entities inside the JSON text are
  never mis-decoded.
- A JSON-LD block may be a bare JobPosting object, an @graph-wrapped document, or a list of
  objects — _job_postings_in() recurses through all three shapes so a careers page listing many
  openings (one JobPosting per posting, in one script or many) and a single-posting page both work
  with the same code path.
- probe() reports confidence=0.6 (below the 1.0 every ATS-API-confirmed adapter reports): this is
  a heuristic HTML scrape, not a confirmed API match, so resolve.py should weight it lower.
- Same non-conflation as the other adapters: _fetch_text()'s "unavailable" status (transient
  network/5xx flake) raises RuntimeError rather than being treated as "no JobPosting present"; a
  404/error, or a 2xx page with no JSON-LD JobPosting block at all, is a routine "not found".
"""
import hashlib
import json
import time
from html.parser import HTMLParser
from typing import Any, Optional

import requests

from pipeline.jobs.ats import BoardInfo, RawPosting

_HTTP_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_USER_AGENT = "SignalSlate JobsCollector/1.0 (+https://github.com/lasswellt/signalslate)"

_PROBE_CONFIDENCE = 0.6

# Indirection so tests neither wait nor depend on real timing (pipeline/jobs/ats's own pattern).
_sleep = time.sleep


def _retry_after_seconds(response: "requests.Response") -> Optional[float]:
    """The Retry-After header's value in seconds, or None when absent/unparseable (only the
    delta-seconds form is handled, matching pipeline.jobs.ats._retry_after_seconds)."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _fetch_text(url: str) -> tuple[Optional[str], str, Optional[str]]:
    """
    GETs url with up to _MAX_ATTEMPTS tries, retrying a transient failure with backoff.

    Args:
        url: The page to GET.

    Returns:
        (text, status, error), mirroring pipeline.jobs.ats.fetch_json()'s contract but returning
        the raw response body instead of decoded JSON:
          - status="ok": text is the response body; error is None.
          - status="unavailable": every attempt failed transiently (timeout, connection error, or
            a retryable 429/5xx status); text is None and error names the last failure. A
            retryable response carrying a Retry-After header sleeps that long instead of the fixed
            backoff before the next attempt.
          - status="error": the request failed for a non-transient reason (a non-retryable HTTP
            status, e.g. 404); text is None and error names it.
    """
    headers = {"User-Agent": _USER_AGENT}
    last_error: Optional[str] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            if attempt < _MAX_ATTEMPTS - 1:
                _sleep(_BACKOFF_SECONDS)
            continue

        if response.status_code in _RETRYABLE_STATUS:
            last_error = f"HTTP{response.status_code}"
            if attempt < _MAX_ATTEMPTS - 1:
                retry_after = _retry_after_seconds(response)
                _sleep(retry_after if retry_after is not None else _BACKOFF_SECONDS)
            continue

        if response.status_code >= 400:
            return None, "error", f"HTTP{response.status_code}"

        return response.text, "ok", None

    return None, "unavailable", last_error


class _JsonLdExtractor(HTMLParser):
    """Collects the raw text content of every <script type="application/ld+json"> block in an
    HTML document, in document order. HTMLParser scans script content as CDATA (HTML5 content
    model), so the JSON text reaches handle_data() without entity mis-decoding."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._in_ld_json = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() != "script":
            return
        script_type = (dict(attrs).get("type") or "").strip().lower()
        if script_type == "application/ld+json":
            self._in_ld_json = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_ld_json:
            self.blocks.append("".join(self._buffer))
            self._in_ld_json = False
            self._buffer = []


def _extract_json_ld(html_text: str) -> list[Any]:
    """Every <script type="application/ld+json"> block in html_text, parsed as JSON. A block that
    isn't valid JSON is skipped rather than aborting extraction of the others."""
    extractor = _JsonLdExtractor()
    extractor.feed(html_text)
    parsed = []
    for block in extractor.blocks:
        try:
            parsed.append(json.loads(block))
        except ValueError:
            continue
    return parsed


def _job_postings_in(node: Any) -> list[dict]:
    """Every schema.org JobPosting object found within node, recursing into @graph arrays and
    plain lists: a single parsed JSON-LD block may be a bare JobPosting, an @graph-wrapped
    document, or a list of postings."""
    results: list[dict] = []
    if isinstance(node, dict):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "JobPosting" in types:
            results.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                results.extend(_job_postings_in(item))
    elif isinstance(node, list):
        for item in node:
            results.extend(_job_postings_in(item))
    return results


def _external_id(node: dict, page_url: str) -> str:
    """node's identifier.value (schema.org PropertyValue shape) or a bare string identifier,
    falling back to a stable hash of title+url when node carries no identifier field at all."""
    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        value = identifier.get("value")
        if value:
            return str(value)
    elif isinstance(identifier, str) and identifier:
        return identifier
    posting_url = node.get("url") or page_url
    title = node.get("title") or node.get("name") or ""
    return hashlib.sha1(f"{title}|{posting_url}".encode("utf-8")).hexdigest()


def _location(node: dict) -> Optional[str]:
    """"{addressLocality}, {addressRegion}" from node's jobLocation.address, just one of them, or
    None when jobLocation/address is absent or not shaped as expected. jobLocation may be a single
    object or a list of them (multi-location postings); the first is used."""
    job_location = node.get("jobLocation")
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if not isinstance(job_location, dict):
        return None
    address = job_location.get("address")
    if not isinstance(address, dict):
        return None
    parts = [
        part for part in (address.get("addressLocality"), address.get("addressRegion")) if part
    ]
    return ", ".join(parts) if parts else None


def _posting_from_node(node: dict, page_url: str) -> RawPosting:
    """One schema.org JobPosting object mapped to a RawPosting.

    Args:
        node: A parsed JSON-LD object with "@type" including "JobPosting".
        page_url: The careers page URL the JSON-LD was found on, used as a fallback for both
            external_id and url when node doesn't carry its own.

    Returns:
        RawPosting with external_id from identifier.value/identifier (else a hash of title+url),
        title, url (node's own else page_url), description, location from jobLocation.address,
        and posted_at from datePosted.
    """
    return RawPosting(
        external_id=_external_id(node, page_url),
        title=node.get("title") or node.get("name") or "",
        url=node.get("url") or page_url,
        description=node.get("description") or "",
        location=_location(node),
        posted_at=node.get("datePosted"),
    )


class JsonLdAdapter:
    """AtsAdapter for the generic schema.org JobPosting JSON-LD fallback."""

    kind = "jsonld"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        Always None: an arbitrary company's own careers page has no distinctive URL shape to
        pattern-match by (unlike the other 10 ATS-hosted adapters). This adapter is only ever
        reached via the resolver ladder's HTML-fingerprint step, never match_any_url()'s
        URL-pattern pass.

        Args:
            url: A candidate URL (unused).

        Returns:
            None, always.
        """
        return None

    def probe(self, url: str) -> Optional[BoardInfo]:
        """
        Confirms url's HTML embeds at least one schema.org JobPosting JSON-LD block.

        Args:
            url: A careers-page URL to check.

        Returns:
            BoardInfo(board_id=url, confidence=0.6) when at least one JSON-LD block on the page
            parses to (or contains, e.g. inside an @graph) a JobPosting object; None when the page
            fetch fails (a 404/non-2xx) or no JobPosting JSON-LD is found.

        Raises:
            RuntimeError: _fetch_text reports "unavailable" (a transient network/5xx flake) —
            distinct from "no JobPosting present", so the caller shouldn't discard url as a bad
            guess.
        """
        text, status, error = _fetch_text(url)
        if status == "unavailable":
            raise RuntimeError(f"jsonld probe unavailable for {url!r}: {error}")
        if status == "error" or text is None:
            return None
        for block in _extract_json_ld(text):
            if _job_postings_in(block):
                return BoardInfo(board_id=url, confidence=_PROBE_CONFIDENCE)
        return None

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every schema.org JobPosting JSON-LD object embedded in board_id's HTML.

        Args:
            board_id: A confirmed careers-page URL (this adapter's board_id is the URL itself).

        Returns:
            One RawPosting per JobPosting object found across every JSON-LD block on the page, or
            [] when the fetch fails or no JobPosting JSON-LD is present.

        Raises:
            RuntimeError: _fetch_text reports "unavailable" (a transient network/5xx flake) —
            distinct from "no postings present".
        """
        text, status, error = _fetch_text(board_id)
        if status == "unavailable":
            raise RuntimeError(f"jsonld list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or text is None:
            return []
        postings = []
        for block in _extract_json_ld(text):
            for node in _job_postings_in(block):
                postings.append(_posting_from_node(node, board_id))
        return postings


adapter = JsonLdAdapter()
