"""
Company watchlist seeding for pipeline/jobs/: manual add/CSV import, plus two opt-in
company-discovery importers (YC "hiring now" list, HN "Who is hiring" thread) that feed the same
add_company() upsert.

Design decisions:

- add_company() upserts by domain when given (case-insensitive JobCompany.domain match), else by
  normalized name (case-insensitive JobCompany.name match), so re-running any importer never
  creates a duplicate row for the same company — it just refreshes last_seen. Mirrors
  pipeline/domains/inventory.py's add_manual()/import_csv() shape, except the session is passed in
  by the caller rather than opened internally: this module has no request/CLI entrypoint of its
  own (yet), it's called by whatever T-015 wires up.
- import_csv() is import_csv() (domains)'s twin: one company per line, "name" or "name,domain",
  "#"-comments and blank lines skipped, every row independent (a bad row is rejected+reported, not
  fatal to the rest of the import).
- import_yc()/import_hn() are both opt-in, best-effort: a fetch_json() "unavailable"/"error"
  degrades to an empty/partial ImportResult rather than raising, matching pipeline/jobs/ats's
  "a failed external call degrades, it never kills the run" convention (fetch_json's own
  docstring). Neither is wired to a scheduler here — that's an explicit task boundary; both are
  invoked only by an explicit API/UI opt-in action.
- import_yc() caps how many hiring.json entries it processes (_MAX_YC_IMPORT) so one call can't
  flood the DB or run forever against a growing static list.
- import_hn() only extracts a best-effort (company_name, ats_kind, board_id) hint per comment, when
  match_any_url() recognizes a known-ATS URL in the comment body; it never calls resolve_company()
  itself (T-011's resolver) — consuming these hints is T-015's job per the task notes — so
  ImportResult here grows a `hints` field the importer populates but does not otherwise act on.
"""
import html
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from sqlmodel import func, select

from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import JobCompany
from pipeline.domains import normalize_domain
from pipeline.jobs.ats import fetch_json, match_any_url

_log = logging.getLogger(__name__)

_MAX_CSV_FIELDS = 2
_SOURCE_VALUES = ("watchlist", "yc", "hn", "email", "manual")

_YC_HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
# Sane cap so one opt-in import_yc() call can't flood the DB or run forever against a growing
# static list (~2.5MB / hundreds of companies per the research doc).
_MAX_YC_IMPORT = 300

_HN_LATEST_THREAD_URL = "https://hn.algolia.com/api/v1/search_by_date"
_HN_ITEM_URL = "https://hn.algolia.com/api/v1/items/{object_id}"
# Cap on how many top-level comments one import_hn() call walks.
_MAX_HN_COMMENTS = 500

# HN's "Who is hiring" convention: a top-level comment usually opens "Company | Location | ..." or
# "Company (stage) | ...". Best-effort split on the first "|"/newline/tag of the comment's plain
# text — inherently fuzzy, per the task notes.
_HN_NAME_SPLIT_RE = re.compile(r"^\s*([^|\n]{2,80}?)\s*(?:\||\n|$)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r'https?://[^\s"\'<>]+')


@dataclass(frozen=True)
class ImportResult:
    """import_csv()/import_yc()/import_hn()'s outcome: names actually added/updated, every
    rejected row, and (import_hn() only) resolver hints extracted from comment bodies."""
    added: list[str] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (1-based line number, reason)
    hints: list[tuple[str, str, str]] = field(default_factory=list)  # (company_name, ats_kind, board_id)


def _domain_from_website(website: str) -> Optional[str]:
    """Best-effort normalized domain from a website URL/bare-domain string, or None when it's
    missing/unparseable — never raises, since import_yc() must skip a malformed entry, not fail
    the whole import."""
    if not website:
        return None
    candidate = website.strip()
    if not candidate:
        return None
    if "//" not in candidate:
        candidate = f"//{candidate}"
    parsed = urlparse(candidate)
    host = parsed.netloc or parsed.path
    if not host:
        return None
    try:
        normalized, _tld = normalize_domain(host)
        return normalized
    except ValueError:
        return None


def add_company(session: "db.Session", name: str, domain: Optional[str] = None, source: str = "manual") -> JobCompany:
    """
    Adds or updates one watchlist company, upserting by domain (case-insensitive) when given, else
    by normalized name (case-insensitive) — never creates a duplicate row for the same company
    across repeated importer runs.

    Args:
        session: An open SQLModel Session.
        name: Company name as reported by the source.
        domain: Company domain, when known (e.g. derived from a careers-page/website URL).
        source: One of watchlist | yc | hn | email | manual (JobCompany.source's documented values).

    Returns:
        The stored JobCompany row (added to session; caller commits).

    Raises:
        ValueError: name is empty/blank, or source is not one of the documented values.
    """
    normalized_name = name.strip() if isinstance(name, str) else ""
    if not normalized_name:
        raise ValueError("name must not be empty")
    if source not in _SOURCE_VALUES:
        raise ValueError(f"source must be one of {_SOURCE_VALUES}: {source!r}")

    row: Optional[JobCompany] = None
    if domain:
        row = session.exec(select(JobCompany).where(func.lower(JobCompany.domain) == domain.lower())).first()
    if row is None:
        row = session.exec(select(JobCompany).where(func.lower(JobCompany.name) == normalized_name.lower())).first()

    now = utcnow()
    if row is None:
        row = JobCompany(name=normalized_name, domain=domain, source=source, status="active", first_seen=now)
    row.last_seen = now
    session.add(row)
    return row


def import_csv(session: "db.Session", text: str) -> ImportResult:
    """
    Bulk add_company(source="watchlist") from CSV/plain text: one company per line, "name" or
    "name,domain". Blank lines and "#"-comment lines are skipped. Every row is independent, so one
    bad row (e.g. empty name) is rejected and reported while the rest of the import still runs.

    Args:
        session: An open SQLModel Session.
        text: The raw CSV/plain-text body.

    Returns:
        ImportResult: names actually added/updated, and (1-based line_number, reason) for every
        rejected row.
    """
    result = ImportResult()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) > _MAX_CSV_FIELDS:
            result.rejected.append((line_number, "too many fields"))
            continue
        name = parts[0]
        domain = parts[1] if len(parts) > 1 and parts[1] else None
        try:
            row = add_company(session, name, domain=domain, source="watchlist")
        except ValueError as exc:
            result.rejected.append((line_number, str(exc)))
            continue
        result.added.append(row.name)
    return result


def import_yc(session: "db.Session", *, client=None) -> ImportResult:
    """
    Imports up to _MAX_YC_IMPORT companies from yc-oss's static "hiring now" list. Opt-in: never
    invoked by the scheduler, only by an explicit API/UI action.

    Args:
        session: An open SQLModel Session.
        client: Optional fetch_json-shaped callable (url, **kwargs) -> (data, status, error),
            injected in tests instead of hitting the network. Defaults to
            pipeline.jobs.ats.fetch_json.

    Returns:
        ImportResult: companies added/updated (source="yc"). Empty when the list is unreachable,
        empty, or shaped unexpectedly — a fetch_json "unavailable"/"error" degrades here, it never
        raises past this module.
    """
    fetch = client or fetch_json
    data, status, error = fetch(_YC_HIRING_URL)
    result = ImportResult()
    if status != "ok":
        _log.warning("jobs seeds: yc import failed: %s", error)
        return result
    if not isinstance(data, list):
        _log.warning("jobs seeds: yc import got unexpected payload shape: %s", type(data).__name__)
        return result

    for entry in data[:_MAX_YC_IMPORT]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name or not isinstance(name, str):
            continue
        domain = _domain_from_website(entry.get("website") or "")
        try:
            row = add_company(session, name, domain=domain, source="yc")
        except ValueError:
            continue
        result.added.append(row.name)
    return result


def _extract_hn_name(text: str) -> Optional[str]:
    """Best-effort company name from the start of a "Who is hiring" comment's plain-text body, or
    None when nothing plausible is found."""
    plain = html.unescape(_HTML_TAG_RE.sub(" ", text)).strip()
    if not plain:
        return None
    match = _HN_NAME_SPLIT_RE.match(plain)
    if not match:
        return None
    candidate = match.group(1).strip(" -–—")
    return candidate or None


def import_hn(session: "db.Session") -> ImportResult:
    """
    Imports companies from HN's latest "Who is hiring" thread, plus any known-ATS URL hints found
    in each top-level comment. Opt-in, best-effort: a missing/malformed thread degrades to an
    empty ImportResult, never raises past this module.

    Args:
        session: An open SQLModel Session.

    Returns:
        ImportResult: companies added/updated (source="hn"), plus `hints` — one
        (company_name, ats_kind, board_id) tuple per comment where pipeline.jobs.ats.match_any_url()
        recognized a URL in the comment body. Consuming these hints (T-011's resolve_company()) is
        a later task's job; this only collects them.
    """
    result = ImportResult()
    search_data, status, error = fetch_json(
        _HN_LATEST_THREAD_URL, params={"tags": "story,author_whoishiring", "hitsPerPage": 1}
    )
    if status != "ok":
        _log.warning("jobs seeds: hn thread lookup failed: %s", error)
        return result
    hits = search_data.get("hits") if isinstance(search_data, dict) else None
    if not hits:
        return result
    object_id = hits[0].get("objectID") if isinstance(hits[0], dict) else None
    if not object_id:
        return result

    item_data, status, error = fetch_json(_HN_ITEM_URL.format(object_id=object_id))
    if status != "ok":
        _log.warning("jobs seeds: hn thread fetch failed: %s", error)
        return result
    if not isinstance(item_data, dict):
        return result

    for comment in (item_data.get("children") or [])[:_MAX_HN_COMMENTS]:
        if not isinstance(comment, dict):
            continue
        text = comment.get("text")
        if not text or not isinstance(text, str):
            continue
        name = _extract_hn_name(text)
        if not name:
            continue
        try:
            row = add_company(session, name, source="hn")
        except ValueError:
            continue
        result.added.append(row.name)

        matched = None
        for raw_url in _URL_RE.findall(html.unescape(text)):
            matched = match_any_url(raw_url)
            if matched:
                break
        if matched:
            result.hints.append((row.name, matched[0], matched[1]))

    return result
