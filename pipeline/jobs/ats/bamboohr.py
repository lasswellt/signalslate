"""
BambooHR ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses BambooHR's public, unauthenticated careers-list endpoint (https://{company}.bamboohr.com/
  careers/list) with an explicit Accept: application/json header, since without it the endpoint
  can also serve HTML.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py/rippling.py's pattern.
- Same non-conflation as the other adapters: fetch_json()'s "unavailable" status (transient
  network/5xx flake) raises RuntimeError rather than being treated as "board doesn't exist"; a
  404, or a 2xx body that isn't shaped like a careers-list payload, is a routine "not found".
- The exact shape of BambooHR's careers/list JSON is undocumented (the research doc's live probe
  only confirmed an empty-tenant response: {"meta": {...}, "result": []}); this adapter therefore
  parses leniently — a missing or non-list "result" is treated as "no postings" for
  list_postings() rather than raised as an error, since crashing on an unexpected-but-2xx shape
  would be worse than returning an empty board.
- No per-job detail fetch: BambooHR's own job page is HTML, not JSON (same situation as
  rippling.py), so description is synthesized from the list entry's own department/location
  labels rather than scraped.
"""
import re
from typing import Any, Optional

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_CAREERS_LIST_URL = "https://{company}.bamboohr.com/careers/list"

_URL_PATTERN = re.compile(r"https?://([A-Za-z0-9-]+)\.bamboohr\.com/careers", re.IGNORECASE)


def _result_list(data: Any) -> Optional[list]:
    """The list under data's "result" key, or None when data isn't shaped like a careers-list
    payload (not a dict, or "result" isn't a list)."""
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    return result if isinstance(result, list) else None


def _synthesize_description(department: Optional[str], location: Optional[str]) -> str:
    """A short description built from the list response's own fields, since BambooHR's
    careers-list endpoint carries no job description text and its per-job page is HTML, not
    JSON (see module docstring)."""
    parts = []
    if department:
        parts.append(f"Department: {department}")
    if location:
        parts.append(f"Location: {location}")
    return " | ".join(parts)


class BambooHRAdapter:
    """AtsAdapter for BambooHR-hosted job boards."""

    kind = "bamboohr"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The BambooHR company subdomain url resolves to, or None if url isn't a BambooHR careers
        URL.

        Args:
            url: A candidate careers-page URL, e.g. "https://acme.bamboohr.com/careers/123".

        Returns:
            The company subdomain, or None when url doesn't match the
            {company}.bamboohr.com/careers pattern.
        """
        match = _URL_PATTERN.search(url)
        return match.group(1) if match else None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live BambooHR company subdomain.

        Args:
            slug: A candidate BambooHR company subdomain.

        Returns:
            BoardInfo(board_id=slug, confidence=1.0) when slug's careers-list endpoint responds
            with a parseable (even empty) result list; None when BambooHR reports the board
            doesn't exist (a 404, or a 2xx body that isn't shaped like a careers-list payload).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(
            _CAREERS_LIST_URL.format(company=slug), headers={"Accept": "application/json"}
        )
        if status == "unavailable":
            raise RuntimeError(f"bamboohr probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if _result_list(data) is None:
            return None
        return BoardInfo(board_id=slug, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on BambooHR.

        Args:
            board_id: A confirmed BambooHR company subdomain.

        Returns:
            One RawPosting per job BambooHR's careers-list endpoint returns for board_id, or []
            when the board doesn't exist or its response isn't shaped like a careers-list
            payload (a missing/non-list "result" is "no postings", not an error).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(
            _CAREERS_LIST_URL.format(company=board_id), headers={"Accept": "application/json"}
        )
        if status == "unavailable":
            raise RuntimeError(f"bamboohr list_postings unavailable for {board_id!r}: {error}")
        if status == "error":
            return []

        result = _result_list(data)
        if not result:
            return []

        postings = []
        for job in result:
            if not isinstance(job, dict):
                continue
            department = job.get("departmentLabel")
            location = job.get("locationLabel")
            postings.append(
                RawPosting(
                    external_id=str(job.get("id")),
                    title=job.get("jobOpeningName") or "",
                    url=job.get("jobOpeningShareUrl")
                    or _CAREERS_LIST_URL.format(company=board_id),
                    description=_synthesize_description(department, location),
                    location=location,
                )
            )
        return postings


adapter = BambooHRAdapter()
