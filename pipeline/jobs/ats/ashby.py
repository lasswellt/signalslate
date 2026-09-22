"""
Ashby ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses Ashby's public, unauthenticated job-board API (api.ashbyhq.com/posting-api/job-board/
  {board_name}), not the candidate-facing jobs.ashbyhq.com pages, for structured posting data.
  includeCompensation=true is passed so a future comp_text mapping has the field available, though
  RawPosting.comp_text isn't populated here since the task doesn't call for it.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py/lever.py's pattern and tests/test_jobs_ats_base.py's
  requests-stubbing convention.
- Same non-conflation as greenhouse.py/lever.py: fetch_json()'s "unavailable" status (transient
  flake) raises RuntimeError rather than being treated as "board doesn't exist" (probe -> None,
  list_postings -> []); a 404, or a 2xx body that isn't shaped like a job-board payload (missing
  "jobs", e.g. an org-not-found error shape), is a routine "not found".
- list_postings() skips jobs with isListed is False: Ashby's payload can include unlisted/closed
  jobs alongside open ones, and the AtsAdapter contract is "every open posting".
"""
import re
from typing import Optional

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_JOB_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{board_name}"

_URL_PATTERNS = (
    re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(
        r"https?://api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_-]+)", re.IGNORECASE
    ),
)


class AshbyAdapter:
    """AtsAdapter for Ashby-hosted job boards."""

    kind = "ashby"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Ashby board name url resolves to, or None if url isn't an Ashby URL.

        Args:
            url: A candidate careers-page or API URL.

        Returns:
            The board name (slug), or None when url matches neither the candidate-facing
            jobs.ashbyhq.com host nor the api.ashbyhq.com job-board API host.
        """
        for pattern in _URL_PATTERNS:
            match = pattern.search(url)
            if match:
                return match.group(1)
        return None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live Ashby board name.

        Args:
            slug: A candidate Ashby board name.

        Returns:
            BoardInfo(board_id=slug, company_name=response["name"], confidence=1.0) when slug's
            job-board endpoint responds with a valid job-board payload; None when Ashby reports
            the board doesn't exist (a 404, or a 2xx body with no "jobs" key, e.g. an
            org-not-found error shape).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(
            _JOB_BOARD_URL.format(board_name=slug), params={"includeCompensation": "true"}
        )
        if status == "unavailable":
            raise RuntimeError(f"ashby probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, dict) or "jobs" not in data:
            return None
        return BoardInfo(board_id=slug, company_name=data.get("name"), confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Ashby.

        Args:
            board_id: A confirmed Ashby board name.

        Returns:
            One RawPosting per listed job Ashby's job-board endpoint returns for board_id
            (jobs with isListed is False are skipped), or [] when the board doesn't exist (a
            404, or a 2xx body with no "jobs" key).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(
            _JOB_BOARD_URL.format(board_name=board_id), params={"includeCompensation": "true"}
        )
        if status == "unavailable":
            raise RuntimeError(f"ashby list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or not isinstance(data, dict) or "jobs" not in data:
            return []

        postings = []
        for job in data.get("jobs") or []:
            if job.get("isListed") is False:
                continue
            postings.append(
                RawPosting(
                    external_id=str(job.get("id")),
                    title=job.get("title") or "",
                    url=job.get("jobUrl") or "",
                    description=job.get("description") or "",
                    location=job.get("location"),
                    posted_at=job.get("publishedAt"),
                )
            )
        return postings


adapter = AshbyAdapter()
