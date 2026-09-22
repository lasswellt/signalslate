"""
Greenhouse ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses Greenhouse's public, unauthenticated board API (boards-api.greenhouse.io), not the
  candidate-facing boards.greenhouse.io HTML pages, so list_postings() gets structured job data
  (id/title/absolute_url/location/content/updated_at) instead of scraping HTML.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly) so tests stub the network at pipeline.jobs.ats.requests, matching
  tests/test_jobs_ats_base.py's convention, and so retry/backoff/Retry-After handling is shared.
- fetch_json()'s "unavailable" status (transient network/5xx flake, all retries exhausted) is not
  conflated with "board doesn't exist": probe()/list_postings() raise RuntimeError for it instead
  of returning None/[] , since a caller treating a network flake as "no such board" would
  incorrectly give up on a real board. A 404, or a 2xx response that isn't shaped like a jobs
  board payload, is a routine "not found" per the AtsAdapter.probe() contract and returns
  None/[] without raising.
- company_name is always None in the returned BoardInfo: Greenhouse's public jobs-list endpoint
  doesn't include a company display name field, so there is nothing honest to put there.
"""
import re
from typing import Optional

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"

_URL_PATTERNS = (
    re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.IGNORECASE),
)


class GreenhouseAdapter:
    """AtsAdapter for Greenhouse-hosted job boards."""

    kind = "greenhouse"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Greenhouse board token url resolves to, or None if url isn't a Greenhouse URL.

        Args:
            url: A candidate careers-page or API URL.

        Returns:
            The board token (slug), or None when url matches neither the candidate-facing
            boards.greenhouse.io/job-boards.greenhouse.io hosts nor the boards-api.greenhouse.io
            API host.
        """
        for pattern in _URL_PATTERNS:
            match = pattern.search(url)
            if match:
                return match.group(1)
        return None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live Greenhouse board token.

        Args:
            slug: A candidate Greenhouse board token.

        Returns:
            BoardInfo(board_id=slug, confidence=1.0) when slug's jobs endpoint responds with a
            valid jobs-board payload; None when Greenhouse reports the board doesn't exist (a
            404, or a 2xx body that isn't shaped like a jobs list).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(
            _JOBS_URL.format(board_token=slug), params={"content": "true"}
        )
        if status == "unavailable":
            raise RuntimeError(f"greenhouse probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, dict) or "jobs" not in data:
            return None
        return BoardInfo(board_id=slug, company_name=None, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Greenhouse.

        Args:
            board_id: A confirmed Greenhouse board token.

        Returns:
            One RawPosting per job Greenhouse's jobs endpoint returns for board_id, or [] when
            the board doesn't exist (a 404, or a 2xx body that isn't shaped like a jobs list).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(
            _JOBS_URL.format(board_token=board_id), params={"content": "true"}
        )
        if status == "unavailable":
            raise RuntimeError(f"greenhouse list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or not isinstance(data, dict):
            return []

        postings = []
        for job in data.get("jobs") or []:
            location = job.get("location") or {}
            postings.append(
                RawPosting(
                    external_id=str(job.get("id")),
                    title=job.get("title") or "",
                    url=job.get("absolute_url") or "",
                    description=job.get("content") or "",
                    location=location.get("name"),
                    posted_at=job.get("updated_at"),
                )
            )
        return postings


adapter = GreenhouseAdapter()
