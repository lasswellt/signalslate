"""
Recruitee ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses Recruitee's public, unauthenticated offers endpoint (https://{company}.recruitee.com/api/
  offers/), which unlike rippling.py/bamboohr.py already carries a per-offer HTML description in
  the list response — no per-job detail fetch is needed.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py/rippling.py's pattern.
- Same non-conflation as the other adapters: fetch_json()'s "unavailable" status (transient
  network/5xx flake) raises RuntimeError rather than being treated as "board doesn't exist".
  Recruitee's 404-on-unknown-company behavior is what the research doc verifies, so a 404 is
  "not found"; a 2xx response (even one whose "offers" list is empty, e.g. a real company with no
  current openings) counts as "found" per probe()'s contract.
- location is formatted from the offer's own city/country fields rather than a single combined
  field, since the research doc's live-probed shape carries them separately.
"""
import re
from typing import Any, Optional

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_OFFERS_URL = "https://{company}.recruitee.com/api/offers/"

_URL_PATTERN = re.compile(r"https?://([A-Za-z0-9-]+)\.recruitee\.com", re.IGNORECASE)


def _offers_list(data: Any) -> Optional[list]:
    """The list under data's "offers" key, or None when data isn't shaped like an offers-list
    payload (not a dict, or "offers" isn't a list)."""
    if not isinstance(data, dict):
        return None
    offers = data.get("offers")
    return offers if isinstance(offers, list) else None


def _format_location(city: Optional[str], country: Optional[str]) -> Optional[str]:
    """"{city}, {country}", just the city, just the country, or None when neither is present."""
    parts = [part for part in (city, country) if part]
    return ", ".join(parts) if parts else None


class RecruiteeAdapter:
    """AtsAdapter for Recruitee-hosted job boards."""

    kind = "recruitee"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The Recruitee company subdomain url resolves to, or None if url isn't a Recruitee URL.

        Args:
            url: A candidate careers-page URL, e.g. "https://acme.recruitee.com/o/engineer".

        Returns:
            The company subdomain, or None when url doesn't match the {company}.recruitee.com
            pattern.
        """
        match = _URL_PATTERN.search(url)
        return match.group(1) if match else None

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """
        Confirms slug is a live Recruitee company subdomain.

        Args:
            slug: A candidate Recruitee company subdomain.

        Returns:
            BoardInfo(board_id=slug, confidence=1.0) when slug's offers endpoint responds with a
            parseable (even empty) offers list; None when Recruitee reports the board doesn't
            exist (a 404, or a 2xx body that isn't shaped like an offers-list payload).

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard slug as a bad
            guess.
        """
        data, status, error = fetch_json(_OFFERS_URL.format(company=slug))
        if status == "unavailable":
            raise RuntimeError(f"recruitee probe unavailable for {slug!r}: {error}")
        if status == "error":
            return None
        if _offers_list(data) is None:
            return None
        return BoardInfo(board_id=slug, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on Recruitee.

        Args:
            board_id: A confirmed Recruitee company subdomain.

        Returns:
            One RawPosting per offer Recruitee's offers endpoint returns for board_id, or [] when
            the board doesn't exist or its response isn't shaped like an offers-list payload.

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board has no postings".
        """
        data, status, error = fetch_json(_OFFERS_URL.format(company=board_id))
        if status == "unavailable":
            raise RuntimeError(f"recruitee list_postings unavailable for {board_id!r}: {error}")
        if status == "error":
            return []

        offers = _offers_list(data)
        if not offers:
            return []

        postings = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            postings.append(
                RawPosting(
                    external_id=str(offer.get("id")),
                    title=offer.get("title") or "",
                    url=offer.get("careers_apply_url") or "",
                    description=offer.get("description") or "",
                    location=_format_location(offer.get("city"), offer.get("country")),
                    posted_at=offer.get("published_at") or offer.get("created_at"),
                )
            )
        return postings


adapter = RecruiteeAdapter()
