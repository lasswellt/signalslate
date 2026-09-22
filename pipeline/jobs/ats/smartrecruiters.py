"""
SmartRecruiters ATS adapter (pipeline.jobs.ats AtsAdapter Protocol).

Design decisions:

- Uses SmartRecruiters' public, unauthenticated postings API (api.smartrecruiters.com/v1/companies/
  {company_id}/postings), not the candidate-facing jobs.smartrecruiters.com HTML pages.
- probe()/list_postings() route entirely through pipeline.jobs.ats.fetch_json() (never call
  requests directly), matching greenhouse.py/workable.py's pattern and
  tests/test_jobs_ats_base.py's requests-stubbing convention.
- Unlike every other adapter here, an unknown company_id does NOT 404: the postings endpoint
  always answers 200, with `totalFound: 0` for a company that doesn't exist. "Not found" is
  therefore `totalFound == 0`, not an HTTP status — probe()/list_postings() check that field
  explicitly instead of relying on fetch_json()'s status="error" branch.
- Same non-conflation as the other adapters: fetch_json()'s "unavailable" status (transient
  network/5xx flake) raises RuntimeError rather than being treated as "board doesn't exist"; a
  malformed 2xx body (missing "totalFound"/"content") is a routine "not found".
- Full description needs a per-posting detail fetch: the postings-list payload carries only
  id/name/refNumber/releasedDate/location/company, not the job ad text. list_postings() therefore
  makes one extra GET per posting, concatenating whichever of jobAd.sections' known text fields
  (jobDescription/qualifications/additionalInformation) are present rather than assuming all three
  exist. A detail fetch that comes back non-"ok" degrades to description="" rather than aborting
  the whole list_postings() call, matching workday.py's per-posting-flake handling.
- Paging uses limit/offset (SmartRecruiters' documented paging params): list_postings() requests
  pages of _PAGE_LIMIT until a page returns fewer than _PAGE_LIMIT items.
"""
from typing import Any, Optional
import re

from pipeline.jobs.ats import BoardInfo, RawPosting, fetch_json

_POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
_POSTING_DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}"
_PAGE_LIMIT = 100

_URL_PATTERN = re.compile(
    r"https?://jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.IGNORECASE
)
_API_URL_PATTERN = re.compile(
    r"https?://api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.IGNORECASE
)

_DESCRIPTION_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


def _format_location(location) -> Optional[str]:
    """"City, Region, Country" from location (a {city, region, country} dict), omitting absent
    parts rather than emitting stray commas, or None when location isn't a dict or has none."""
    if not isinstance(location, dict):
        return None
    parts = [
        part
        for part in (location.get("city"), location.get("region"), location.get("country"))
        if part
    ]
    return ", ".join(parts) if parts else None


def _fetch_description(company_id: str, posting_id: str) -> str:
    """
    The job ad text for a posting, fetched via one extra GET (the postings-list response doesn't
    include it).

    Args:
        company_id: The SmartRecruiters company id.
        posting_id: The posting's id, from the postings-list response.

    Returns:
        The known jobAd.sections text fields (jobDescription/qualifications/
        additionalInformation) present in the detail response, joined with blank lines, or ""
        when the detail call doesn't succeed or the response isn't shaped as expected. A single
        posting's description flake shouldn't discard the rest of the page's postings, so this
        never raises.
    """
    url = _POSTING_DETAIL_URL.format(company_id=company_id, posting_id=posting_id)
    data, status, _error = fetch_json(url)
    if status != "ok" or not isinstance(data, dict):
        return ""
    job_ad = data.get("jobAd")
    if not isinstance(job_ad, dict):
        return ""
    sections = job_ad.get("sections")
    if not isinstance(sections, dict):
        return ""

    texts = []
    for key in _DESCRIPTION_SECTIONS:
        section = sections.get(key)
        if isinstance(section, dict):
            text = section.get("text")
            if text:
                texts.append(text)
    return "\n\n".join(texts)


class SmartRecruitersAdapter:
    """AtsAdapter for SmartRecruiters-hosted job boards."""

    kind = "smartrecruiters"
    apply_mode = "hosted_form"

    def match_url(self, url: str) -> Optional[str]:
        """
        The SmartRecruiters company id url resolves to, or None if url isn't a SmartRecruiters
        URL.

        Args:
            url: A candidate careers-page or API URL.

        Returns:
            The company id, or None when url matches neither the candidate-facing
            jobs.smartrecruiters.com/{company_id} host nor the api.smartrecruiters.com/v1/
            companies/{company_id} API host.
        """
        match = _URL_PATTERN.search(url)
        if match:
            return match.group(1)
        match = _API_URL_PATTERN.search(url)
        return match.group(1) if match else None

    def probe(self, company_id: str) -> Optional[BoardInfo]:
        """
        Confirms company_id is a live SmartRecruiters company.

        Args:
            company_id: A candidate SmartRecruiters company id.

        Returns:
            BoardInfo(board_id=company_id, company_name=<first posting's company.name, if
            present>, confidence=1.0) when company_id's postings endpoint reports at least one
            posting; None when the endpoint reports `totalFound: 0` (SmartRecruiters' way of
            saying company_id doesn't exist — it never 404s) or a 2xx body that isn't shaped like
            a postings-list payload.

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) —
            distinct from "board doesn't exist", so the caller shouldn't discard company_id as a
            bad guess.
        """
        data, status, error = fetch_json(
            _POSTINGS_URL.format(company_id=company_id), params={"limit": 1}
        )
        if status == "unavailable":
            raise RuntimeError(f"smartrecruiters probe unavailable for {company_id!r}: {error}")
        if status == "error":
            return None
        if not isinstance(data, dict) or "totalFound" not in data or "content" not in data:
            return None
        if not data.get("totalFound"):
            return None

        content = data.get("content") or []
        company_name = None
        if content and isinstance(content[0], dict):
            company = content[0].get("company")
            if isinstance(company, dict):
                company_name = company.get("name")
        return BoardInfo(board_id=company_id, company_name=company_name, confidence=1.0)

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """
        Every open posting board_id currently lists on SmartRecruiters.

        Args:
            board_id: A confirmed SmartRecruiters company id.

        Returns:
            One RawPosting per job across every page of board_id's postings endpoint (paged by
            offset in steps of _PAGE_LIMIT until a page returns fewer than _PAGE_LIMIT items), or
            [] when the endpoint reports `totalFound: 0` or a 2xx body that isn't shaped like a
            postings-list payload.

        Raises:
            RuntimeError: fetch_json reports "unavailable" (a transient network/5xx flake) for a
            postings-list page — distinct from "board has no postings".
        """
        postings: list[RawPosting] = []
        offset = 0

        while True:
            data, status, error = fetch_json(
                _POSTINGS_URL.format(company_id=board_id),
                params={"limit": _PAGE_LIMIT, "offset": offset},
            )
            if status == "unavailable":
                raise RuntimeError(
                    f"smartrecruiters list_postings unavailable for {board_id!r}: {error}"
                )
            if status == "error" or not isinstance(data, dict) or "content" not in data:
                break
            if not data.get("totalFound"):
                break

            page: list[Any] = data.get("content") or []
            if not page:
                break

            for job in page:
                posting_id = str(job.get("id"))
                postings.append(
                    RawPosting(
                        external_id=posting_id,
                        title=job.get("name") or "",
                        url=f"https://jobs.smartrecruiters.com/{board_id}/{posting_id}",
                        description=_fetch_description(board_id, posting_id),
                        location=_format_location(job.get("location")),
                        posted_at=job.get("releasedDate"),
                    )
                )

            if len(page) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT

        return postings


adapter = SmartRecruitersAdapter()
