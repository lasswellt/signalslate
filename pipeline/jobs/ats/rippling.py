"""
Rippling ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses Rippling's undocumented-but-live board API (ats.rippling.com/api/v2/board/{slug}/jobs),
  the same JSON call the candidate-facing ats.rippling.com/{slug}/jobs pages make.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py/workable.py's pattern and
  tests/test_jobs_ats_base.py's requests-stubbing convention.
- Same non-conflation as the other adapters: fetch_json()'s "unavailable" status (transient
  network/5xx flake) raises RuntimeError rather than being treated as "board doesn't exist"; a
  404, or a 2xx body that isn't shaped like a jobs-list payload, is a routine "not found".
- No description field, no detail fetch: research doc (docs/_research/2026-09-21_jobs-collector.md
  ~line 555, Q "Rippling description availability") confirms the jobs-list response carries no
  description, and each item's own `url` is the hosted HTML job page, not a JSON detail endpoint —
  scraping that HTML or inventing an unverified detail-JSON endpoint is out of scope here.
  description is therefore synthesized from the fields the list response actually provides
  (department + locations) rather than left blank, so downstream fit-scoring still has something
  to work with.
- No paging: the research doc's live-probed shape is a flat `items` list with no offset/limit or
  total-count field, so list_postings() makes a single request.
"""
from typing import Optional
import re

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_JOBS_URL = "https://ats.rippling.com/api/v2/board/{slug}/jobs"

_URL_PATTERN = re.compile(r"https?://ats\.rippling\.com/([A-Za-z0-9_-]+)/jobs", re.IGNORECASE)


def _location_names(locations) -> list[str]:
    """The display name of each entry in locations (a list of either strings or {"name": ...}-
    shaped dicts), skipping any entry that yields nothing usable."""
    if not isinstance(locations, list):
        return []
    names = []
    for entry in locations:
        if isinstance(entry, str) and entry:
            names.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name")
            if name:
                names.append(name)
    return names


def _synthesize_description(department, location_names: list[str]) -> str:
    """A short description built from the list response's own fields, since Rippling's jobs-list
    endpoint provides no description text (see module docstring)."""
    parts = []
    if department:
        parts.append(f"Department: {department}")
    if location_names:
        parts.append(f"Locations: {', '.join(location_names)}")
    return " | ".join(parts)


class RipplingAdapter:
    """AtsAdapter for Rippling-hosted job boards."""

    kind = "rippling"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Rippling board slug url resolves to, or None if url isn't a Rippling URL.

        Args:
            url: A candidate careers-page URL, e.g. "https://ats.rippling.com/acme/jobs/123".

        Returns:
            The board slug, or None when url doesn't match the candidate-facing
            ats.rippling.com/{slug}/jobs pattern.
        """
        match = _URL_PATTERN.search(url)
        return match.group(1) if match else None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live Rippling board.

        Args:
            slug: A candidate Rippling board slug.

        Returns:
            BoardInfo(board_id=slug, confidence=1.0) when slug's jobs endpoint responds with a
            valid jobs-list payload; None when Rippling reports the board doesn't exist (a 404,
            or a 2xx body that isn't shaped like a jobs-list payload).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(_JOBS_URL.format(slug=slug))
        if status == "unavailable":
            raise RuntimeError(f"rippling probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, dict) or "items" not in data:
            return None
        return BoardInfo(board_id=slug, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Rippling.

        Args:
            board_id: A confirmed Rippling board slug.

        Returns:
            One RawPosting per job Rippling's jobs endpoint returns for board_id, or [] when the
            board doesn't exist (a 404, or a 2xx body that isn't shaped like a jobs-list payload).
            posted_at is always None: the list response carries no date field.

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(_JOBS_URL.format(slug=board_id))
        if status == "unavailable":
            raise RuntimeError(f"rippling list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or not isinstance(data, dict) or "items" not in data:
            return []

        postings = []
        for job in data.get("items") or []:
            location_names = _location_names(job.get("locations"))
            postings.append(
                RawPosting(
                    external_id=str(job.get("id")),
                    title=job.get("name") or "",
                    url=job.get("url") or "",
                    description=_synthesize_description(job.get("department"), location_names),
                    location=", ".join(location_names) if location_names else None,
                    posted_at=None,
                )
            )
        return postings


adapter = RipplingAdapter()
