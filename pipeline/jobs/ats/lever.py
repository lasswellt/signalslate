"""
Lever ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses Lever's public, unauthenticated postings API (api.lever.co/v0/postings/{company}), not the
  candidate-facing jobs.lever.co pages, for structured posting data.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py's pattern and tests/test_jobs_ats_base.py's
  requests-stubbing convention.
- Same non-conflation as greenhouse.py: fetch_json()'s "unavailable" status (transient flake)
  raises RuntimeError rather than being treated as "board doesn't exist" (probe -> None,
  list_postings -> []); only a 404 or a malformed/non-list 2xx body is a routine "not found".
- createdAt is epoch milliseconds; posted_at is normalized to an ISO 8601 UTC string so
  RawPosting.posted_at is a string across every adapter, not a mix of ints and strings.
"""
import re
from datetime import datetime, timezone
from typing import Optional

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_POSTINGS_URL = "https://api.lever.co/v0/postings/{company}"

_URL_PATTERNS = (
    re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"https?://api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.IGNORECASE),
)


def _epoch_ms_to_iso(value) -> Optional[str]:
    """value (epoch milliseconds) as an ISO 8601 UTC string, or None when value isn't numeric."""
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


class LeverAdapter:
    """AtsAdapter for Lever-hosted job boards."""

    kind = "lever"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Lever company slug url resolves to, or None if url isn't a Lever URL.

        Args:
            url: A candidate careers-page or API URL.

        Returns:
            The company slug, or None when url matches neither the candidate-facing
            jobs.lever.co host nor the api.lever.co API host.
        """
        for pattern in _URL_PATTERNS:
            match = pattern.search(url)
            if match:
                return match.group(1)
        return None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live Lever company slug.

        Args:
            slug: A candidate Lever company slug.

        Returns:
            BoardInfo(board_id=slug, confidence=1.0) when slug's postings endpoint responds with
            a valid postings-list payload; None when Lever reports the board doesn't exist (a
            404, or a 2xx body that isn't a JSON array).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(
            _POSTINGS_URL.format(company=slug), params={"mode": "json"}
        )
        if status == "unavailable":
            raise RuntimeError(f"lever probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, list):
            return None
        return BoardInfo(board_id=slug, company_name=None, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Lever.

        Args:
            board_id: A confirmed Lever company slug.

        Returns:
            One RawPosting per posting Lever's postings endpoint returns for board_id, or []
            when the board doesn't exist (a 404, or a 2xx body that isn't a JSON array).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(
            _POSTINGS_URL.format(company=board_id), params={"mode": "json"}
        )
        if status == "unavailable":
            raise RuntimeError(f"lever list_postings unavailable for {board_id!r}: {error}")
        if status == "error" or not isinstance(data, list):
            return []

        postings = []
        for posting in data:
            categories = posting.get("categories") or {}
            postings.append(
                RawPosting(
                    external_id=str(posting.get("id")),
                    title=posting.get("text") or "",
                    url=posting.get("hostedUrl") or "",
                    description=posting.get("descriptionPlain") or posting.get("description") or "",
                    location=categories.get("location"),
                    posted_at=_epoch_ms_to_iso(posting.get("createdAt")),
                )
            )
        return postings


adapter = LeverAdapter()
