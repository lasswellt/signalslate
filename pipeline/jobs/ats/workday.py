"""
Workday CXS ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Workday has no single-token board slug: a tenant's career site is addressed by three parts
  baked into both the hostname and the API path — tenant (company id), wdN (Workday's numbered
  subdomain, e.g. "wd1", "wd5"), and site (career-site name, often "External" or a custom name).
  board_id is the pipe-joined string "{tenant}|{wdN}|{site}" so resolve.py/inventory.py can keep
  storing/passing a single board_id string like every other adapter; _split_board_id()/
  match_url() are the only places that ever join/split it.
- Workday's jobs-list endpoint requires POST with a JSON body, unlike every other adapter here
  which uses pipeline.jobs.ats.fetch_json()'s GET-only helper. _post_json() is a module-local
  mirror of fetch_json() (_MAX_ATTEMPTS/_RETRYABLE_STATUS/_sleep/Retry-After pattern) rather than
  a change to pipeline/jobs/ats/__init__.py, since that file is out of scope for this adapter and
  no other adapter needs a POST helper.
- Per-posting description fetch: the jobPostings list response only carries title/location/
  postedOn/bulletFields, not the full HTML description, so list_postings() makes one extra GET
  per posting (unlike workable.py, where the widget endpoint already includes a usable
  description and an extra call was explicitly not worth it). This GET is a plain fetch_json()
  call (no POST needed) and is sequential, matching the fact that paging itself is already
  sequential here.
- probe() takes the full "tenant|wdN|site" board_id, not a bare slug: Workday has no cheaper
  single-string probe, so resolve.py is expected to call this only once it already has a way to
  guess all three parts (a URL match or research), unlike Greenhouse/Lever/Ashby/Workable's
  single-slug probe().
- external_id is the trailing "R-<digits>" requisition id pulled out of externalPath when present
  (Workday's normal, stable requisition-number shape), else externalPath itself — either way,
  stable across polls, which is the only contract RawPosting.external_id needs to satisfy.
- Same non-conflation as the other adapters: _post_json()'s "unavailable" status (transient
  network/5xx flake) raises RuntimeError rather than being treated as "board doesn't exist" (probe
  -> None, list_postings -> []); a 404/error, or a 2xx body that isn't shaped like a Workday CXS
  jobs payload, is a routine "not found". A malformed board_id (not a 3-part "a|b|c" string) is
  handled the same way as a not-found board (probe -> None, list_postings -> []) rather than
  raising, since it is caller error, not a network condition.
- A per-posting description fetch that comes back non-"ok" degrades to description="" rather than
  aborting the whole list_postings() call: one posting's transient description flake shouldn't
  discard every other posting already fetched on the same page.
"""
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_HTTP_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_PAGE_LIMIT = 20

_USER_AGENT = "SignalSlate JobsCollector/1.0 (+https://github.com/lasswellt/signalslate)"

# Indirection so tests neither wait nor depend on real timing (pipeline/jobs/ats's own pattern).
_sleep = time.sleep

_JOBS_URL = "https://{tenant}.{wd_n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
_POSTING_URL = "https://{tenant}.{wd_n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
_CAREERS_URL = "https://{tenant}.{wd_n}.myworkdayjobs.com/{site}{external_path}"

_HOST_PATTERN = re.compile(r"^([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
_LOCALE_SEGMENT_PATTERN = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_REQ_ID_PATTERN = re.compile(r"R-\d+", re.IGNORECASE)


def _split_board_id(board_id: str) -> tuple[str, str, str]:
    """
    (tenant, wd_n, site) from a "tenant|wdN|site" board_id.

    Args:
        board_id: A Workday board_id, pipe-joined as "tenant|wdN|site".

    Returns:
        (tenant, wd_n, site).

    Raises:
        ValueError: board_id isn't a 3-part, all-non-empty "a|b|c" string.
    """
    parts = board_id.split("|")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"malformed workday board_id: {board_id!r}")
    return parts[0], parts[1], parts[2]


def _external_id_from_path(external_path: str) -> str:
    """The trailing "R-<digits>" requisition id in external_path, or external_path itself when
    no such id is present. Either way stable across polls, which is all RawPosting.external_id
    requires."""
    matches = _REQ_ID_PATTERN.findall(external_path)
    return matches[-1] if matches else external_path


def _retry_after_seconds(response: "requests.Response") -> Optional[float]:
    """The Retry-After header's value in seconds, or None when absent/unparseable. Mirrors
    pipeline.jobs.ats._retry_after_seconds(): only the delta-seconds form is handled."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _post_json(url: str, payload: dict) -> tuple[Any, str, Optional[str]]:
    """
    POSTs url with a JSON body, up to _MAX_ATTEMPTS tries, retrying a transient failure with
    backoff. Mirrors pipeline.jobs.ats.fetch_json()'s contract, but for Workday's CXS API, which
    requires POST rather than GET.

    Args:
        url: The endpoint to POST.
        payload: The JSON request body.

    Returns:
        (data, status, error), matching fetch_json()'s contract:
          - status="ok": data is the response's decoded JSON body; error is None.
          - status="unavailable": every attempt failed transiently (a timeout, connection error,
            or a retryable 429/5xx status); data is None and error is the last exception class
            name or an "HTTP<code>" marker. A retryable response carrying a Retry-After header
            sleeps that long instead of the fixed backoff before the next attempt.
          - status="error": the request failed for a non-transient reason (a non-retryable HTTP
            status) or the response body was not valid JSON; data is None and error names it.
    """
    headers = {"User-Agent": _USER_AGENT, "Content-Type": "application/json"}
    last_error: Optional[str] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT)
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

        try:
            return response.json(), "ok", None
        except ValueError as exc:
            return None, "error", type(exc).__name__

    return None, "unavailable", last_error


def _fetch_description(tenant: str, wd_n: str, site: str, external_path: str) -> str:
    """
    The full HTML job description for a posting, fetched via one extra GET (Workday's jobs-list
    response doesn't include it).

    Args:
        tenant: The Workday tenant id.
        wd_n: The Workday numbered subdomain (e.g. "wd5").
        site: The career-site name.
        external_path: The posting's externalPath from the jobs-list response.

    Returns:
        jobPostingInfo.jobDescription from the posting-detail endpoint, or "" when that call
        doesn't succeed or the response isn't shaped as expected. A single posting's description
        flake shouldn't discard the rest of the page's postings, so this never raises.
    """
    url = _POSTING_URL.format(tenant=tenant, wd_n=wd_n, site=site, external_path=external_path)
    data, status, _error = fetch_json(url)
    if status != "ok" or not isinstance(data, dict):
        return ""
    info = data.get("jobPostingInfo")
    if not isinstance(info, dict):
        return ""
    return info.get("jobDescription") or ""


class WorkdayAdapter:
    """AtsAdapter for Workday CXS-hosted job boards."""

    kind = "workday"
    apply_mode = "account_per_tenant"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Workday "tenant|wdN|site" board_id url resolves to, or None if url isn't a Workday
        careers URL.

        Args:
            url: A candidate careers-page URL, e.g.
                "https://acme.wd5.myworkdayjobs.com/External/job/..." or
                "https://acme.wd5.myworkdayjobs.com/en-US/External/job/...".

        Returns:
            "tenant|wdN|site", or None when url's host isn't
            "{tenant}.{wdN}.myworkdayjobs.com" or its path has no site segment. An optional
            locale segment (e.g. "en-US") immediately before site is skipped.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        host_match = _HOST_PATTERN.match(parsed.hostname)
        if not host_match:
            return None
        tenant, wd_n = host_match.group(1), host_match.group(2).lower()

        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            return None
        site = segments[0]
        if _LOCALE_SEGMENT_PATTERN.match(site):
            if len(segments) < 2:
                return None
            site = segments[1]
        return f"{tenant}|{wd_n}|{site}"

    def probe(self, board_id: str) -> Optional[BoardInfo]:
        """
        Confirms board_id is a live Workday tenant/wdN/site triple.

        Args:
            board_id: A candidate "tenant|wdN|site" triple.

        Returns:
            BoardInfo(board_id=board_id, confidence=1.0) when the triple's jobs endpoint responds
            with a valid CXS jobs payload; None when board_id is malformed, or Workday reports the
            board doesn't exist (a 404, or a 2xx body that isn't shaped like a jobs payload).

        Raises:
            RuntimeError: _post_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard board_id as a
            bad guess.
        """
        try:
            tenant, wd_n, site = _split_board_id(board_id)
        except ValueError:
            return None

        url = _JOBS_URL.format(tenant=tenant, wd_n=wd_n, site=site)
        payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        data, status, error = _post_json(url, payload)
        if status == "unavailable":
            raise RuntimeError(f"workday probe unavailable for {board_id!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, dict) or "jobPostings" not in data or "total" not in data:
            return None
        return BoardInfo(board_id=board_id, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Workday.

        Args:
            board_id: A confirmed "tenant|wdN|site" triple.

        Returns:
            One RawPosting per job across every page of board_id's jobs endpoint (paged by offset
            in steps of the page limit until offset >= total or an empty page), or [] when
            board_id is malformed or Workday reports the board doesn't exist (a 404, or a 2xx body
            that isn't shaped like a jobs payload).

        Raises:
            RuntimeError: _post_json reports "unavailable" (a transient network/5xx flake) for a
            jobs-list page — distinct from "board has no postings".
        """
        try:
            tenant, wd_n, site = _split_board_id(board_id)
        except ValueError:
            return []

        jobs_url = _JOBS_URL.format(tenant=tenant, wd_n=wd_n, site=site)
        postings: list[RawPosting] = []
        offset = 0

        while True:
            payload = {
                "appliedFacets": {},
                "limit": _PAGE_LIMIT,
                "offset": offset,
                "searchText": "",
            }
            data, status, error = _post_json(jobs_url, payload)
            if status == "unavailable":
                raise RuntimeError(
                    f"workday list_postings unavailable for {board_id!r}: {error}"
                )
            if status == "error" or not isinstance(data, dict) or "jobPostings" not in data:
                break

            page = data.get("jobPostings") or []
            if not page:
                break

            for job in page:
                external_path = job.get("externalPath") or ""
                description = _fetch_description(tenant, wd_n, site, external_path)
                postings.append(
                    RawPosting(
                        external_id=_external_id_from_path(external_path),
                        title=job.get("title") or "",
                        url=_CAREERS_URL.format(
                            tenant=tenant, wd_n=wd_n, site=site, external_path=external_path
                        ),
                        description=description,
                        location=job.get("locationsText"),
                        posted_at=job.get("postedOn"),
                    )
                )

            offset += _PAGE_LIMIT
            total = data.get("total") or 0
            if offset >= total:
                break

        return postings


adapter = WorkdayAdapter()
