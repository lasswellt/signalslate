"""
Personio ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Personio's public XML job feed (`GET https://{company}.jobs.personio.de/xml`, live-verified in
  this project's research doc, root `<workzag-jobs>` with repeated `<position>` children) is XML,
  not JSON, so this adapter cannot route through pipeline.jobs.ats.fetch_json() (which only
  decodes JSON bodies). _fetch_text() is a module-local mirror of fetch_json()'s retry/backoff/
  Retry-After structure (_MAX_ATTEMPTS/_RETRYABLE_STATUS/_sleep pattern), returning the raw
  response body instead of parsed JSON, matching workday.py's precedent of a module-local helper
  for a request shape pipeline.jobs.ats.fetch_json() doesn't cover.
- Parses with defusedxml.ElementTree, never stdlib xml.etree.ElementTree: this project already
  depends on defusedxml==0.7.1 specifically to avoid XXE/billion-laughs-style entity-expansion
  attacks against untrusted XML fetched from arbitrary company subdomains.
- <position> is read leniently by child tag name (id/name/office/city/department/jobDescriptions/
  createdAt/url) rather than assuming a rigid schema depth, since Personio's feed shape varies
  slightly between tenants and only "repeated <position> elements under the root" is a safe
  assumption. jobDescriptions may nest multiple <jobDescription><value> blocks (rich-text
  sections); their <value> texts are joined with a blank line into RawPosting.description, falling
  back to every text node under jobDescriptions when no <jobDescription>/<value> pair is found.
- probe() distinguishes three outcomes: a 404/non-2xx -> None ("board doesn't exist"); a 2xx body
  that isn't valid XML at all -> None (malformed response, same treatment other adapters give a
  malformed 2xx JSON body); a 2xx body that parses as XML, even with zero <position> children (a
  real company currently listing no openings) -> BoardInfo(board_id=company). "unavailable" (a
  transient network/5xx flake, retries exhausted) raises RuntimeError instead of returning None,
  since a caller treating a flake as "no such board" would incorrectly discard a real one.
- No per-posting fetch: unlike workday.py, the feed's jobDescriptions block already carries a full
  description inline, so list_postings() needs exactly one GET.
"""
import re
import time
from typing import Optional

import requests
from defusedxml.ElementTree import ParseError, fromstring

from pipeline.jobs.ats import BoardInfo, RawPosting

_FEED_URL = "https://{company}.jobs.personio.de/xml"

_URL_PATTERN = re.compile(r"https?://([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.IGNORECASE)

_HTTP_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_USER_AGENT = "SignalSlate JobsCollector/1.0 (+https://github.com/lasswellt/signalslate)"

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
        url: The endpoint to GET.

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


def _extract_description(position) -> str:
    """Every jobDescriptions <jobDescription><value> text under position, joined with a blank
    line, or every text node under jobDescriptions when no jobDescription/value pair is found
    (a tenant whose feed nests the text differently), or "" when jobDescriptions is absent."""
    container = position.find("jobDescriptions")
    if container is None:
        return ""
    parts = [
        value.strip()
        for value in (child.findtext("value") for child in container.findall("jobDescription"))
        if value and value.strip()
    ]
    if parts:
        return "\n\n".join(parts)
    text = "".join(container.itertext()).strip()
    return text


def _position_to_posting(position, company: str) -> RawPosting:
    """One <position> element mapped to a RawPosting.

    Args:
        position: A defusedxml Element for a single <position>.
        company: The Personio company subdomain, used to build a fallback apply URL when the
            position has no <url> of its own.

    Returns:
        RawPosting with external_id from <id>, title from <name>, url from <url> (else a
        constructed job-page URL), description from joined jobDescriptions text, location from
        <office>/<city>, and posted_at from <createdAt>.
    """
    external_id = (position.findtext("id") or "").strip()
    url = position.findtext("url") or f"https://{company}.jobs.personio.de/job/{external_id}"
    return RawPosting(
        external_id=external_id,
        title=position.findtext("name") or "",
        url=url,
        description=_extract_description(position),
        location=position.findtext("office") or position.findtext("city"),
        posted_at=position.findtext("createdAt"),
    )


class PersonioAdapter:
    """AtsAdapter for Personio-hosted job boards."""

    kind = "personio"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Personio company subdomain url resolves to, or None if url isn't a Personio URL.

        Args:
            url: A candidate careers-page URL, e.g. "https://acme.jobs.personio.de/job/123" or
                the .com TLD variant.

        Returns:
            The company subdomain, or None when url doesn't match the
            {company}.jobs.personio.{de,com} pattern.
        """
        match = _URL_PATTERN.search(url)
        return match.group(1) if match else None

    def probe(self, company: str) -> Optional[BoardInfo]:
        """
        Confirms company is a live Personio company subdomain.

        Args:
            company: A candidate Personio company subdomain.

        Returns:
            BoardInfo(board_id=company) when company's XML feed responds 2xx with a parseable XML
            body, even one with zero <position> children (a real company with no current
            openings); None when Personio reports the board doesn't exist (a 404), or a 2xx body
            that isn't valid XML at all (malformed response).

        Raises:
            RuntimeError: _fetch_text reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard company as a bad
            guess.
        """
        text, status, error = _fetch_text(_FEED_URL.format(company=company))
        if status == "unavailable":
            raise RuntimeError(f"personio probe unavailable for {company!r}: {error}")
        if status == "error" or text is None:
            return None
        try:
            fromstring(text)
        except ParseError:
            return None
        return BoardInfo(board_id=company)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Personio.

        Args:
            board_id: A confirmed Personio company subdomain.

        Returns:
            One RawPosting per <position> in board_id's XML feed, or [] when the board doesn't
            exist, has no current openings, or its response isn't valid XML.

        Raises:
            RuntimeError: _fetch_text reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        text, status, error = _fetch_text(_FEED_URL.format(company=board_id))
        if status == "unavailable":
            raise RuntimeError(f"personio list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or text is None:
            return []
        try:
            root = fromstring(text)
        except ParseError:
            return []
        return [
            _position_to_posting(position, board_id) for position in root.findall(".//position")
        ]


adapter = PersonioAdapter()
