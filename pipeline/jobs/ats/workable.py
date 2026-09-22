"""
Workable ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses Workable's public, unauthenticated widget API (apply.workable.com/api/v1/widget/accounts/
  {account_subdomain}), not the candidate-facing apply.workable.com/{account_subdomain} pages, for
  structured posting data.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py/lever.py/ashby.py's pattern and
  tests/test_jobs_ats_base.py's requests-stubbing convention.
- Same non-conflation as the other adapters: fetch_json()'s "unavailable" status (transient
  flake) raises RuntimeError rather than being treated as "board doesn't exist" (probe -> None,
  list_postings -> []); a 404, or a 2xx body with no "jobs" key, is a routine "not found".
- description uses the widget endpoint's own per-job field as-is: the widget response's job
  entries carry a summary/description field, and fetching each job's full HTML individually would
  be an N+1 network call per posting the task explicitly says not to add.
- location is formatted "City, Country" from the job's {city, country} object, omitting either
  part when absent rather than emitting a stray comma.
"""
import re
from typing import Optional

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_WIDGET_URL = "https://apply.workable.com/api/v1/widget/accounts/{account_subdomain}"

_URL_PATTERN = re.compile(r"https?://apply\.workable\.com/(?!api/)([A-Za-z0-9_-]+)", re.IGNORECASE)


def _format_location(location) -> Optional[str]:
    """"City, Country" from location (a {city, country} dict), or None when neither is present."""
    if not isinstance(location, dict):
        return None
    parts = [part for part in (location.get("city"), location.get("country")) if part]
    return ", ".join(parts) if parts else None


class WorkableAdapter:
    """AtsAdapter for Workable-hosted job boards."""

    kind = "workable"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Workable account subdomain url resolves to, or None if url isn't a Workable URL.

        Args:
            url: A candidate careers-page URL.

        Returns:
            The account subdomain, or None when url doesn't match the candidate-facing
            apply.workable.com/{account_subdomain} pattern (a custom-domain Workable board is out
            of scope here). The widget API path (apply.workable.com/api/...) is not matched as a
            careers page.
        """
        match = _URL_PATTERN.search(url)
        return match.group(1) if match else None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live Workable account subdomain.

        Args:
            slug: A candidate Workable account subdomain.

        Returns:
            BoardInfo(board_id=slug, company_name=response["name"], confidence=1.0) when slug's
            widget endpoint responds with a valid job-list payload; None when Workable reports
            the board doesn't exist (a 404, or a 2xx body with no "jobs" key).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(_WIDGET_URL.format(account_subdomain=slug))
        if status == "unavailable":
            raise RuntimeError(f"workable probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, dict) or "jobs" not in data:
            return None
        return BoardInfo(board_id=slug, company_name=data.get("name"), confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Workable.

        Args:
            board_id: A confirmed Workable account subdomain.

        Returns:
            One RawPosting per job Workable's widget endpoint returns for board_id, or [] when
            the board doesn't exist (a 404, or a 2xx body with no "jobs" key).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(_WIDGET_URL.format(account_subdomain=board_id))
        if status == "unavailable":
            raise RuntimeError(f"workable list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or not isinstance(data, dict) or "jobs" not in data:
            return []

        postings = []
        for job in data.get("jobs") or []:
            postings.append(
                RawPosting(
                    external_id=str(job.get("shortcode")),
                    title=job.get("title") or "",
                    url=job.get("url") or "",
                    description=job.get("description") or "",
                    location=_format_location(job.get("location")),
                )
            )
        return postings


adapter = WorkableAdapter()
