"""
JobBoard resolver ladder for pipeline/jobs/: URL pattern -> verified slug probe -> HTML careers
fingerprint -> JSON-LD fallback.

Design decisions:

- resolve_company() runs each step in order and returns the first hit (research doc "Resolution
  steps 1-3" + Risks): a cheap, high-confidence match (an already-known URL) should never be
  shadowed by a more expensive, lower-confidence one (a slug guess or an HTML scrape).
- Step 1 (URL pattern) needs no network call at all: pipeline.jobs.ats.match_any_url() is pure
  string matching against known_urls the caller already has (e.g. a seed importer's careers-page
  link), so it's free and goes first.
- Step 2 (probe_slugs) only tries the 6 ATS kinds whose probe() accepts a bare slug/token guess
  (greenhouse, lever, ashby, workable, smartrecruiters, rippling) — workday needs a tenant/site
  pair, bamboohr/recruitee/personio need more than a guessed slug, and jsonld has no slug concept
  at all (its board_id is a URL). Capped at _MAX_SLUG_PROBES total probes, matching the research
  doc's "at most 5 platforms x 3 slugs = 15 cheap GETs" budget.
- probe_slugs() rejects a name-mismatched hit rather than trusting any 2xx response: the research
  doc's "Risks" section calls out a generic slug (e.g. "engineering") existing on some unrelated
  company's board as a false-positive risk. When the adapter itself returns no company_name to
  check (Greenhouse/Lever/Rippling never do), the hit is accepted but at a lower confidence, since
  there's no independent verification a human reviewing JobBoard.confidence can rely on.
- fingerprint_html() is a coarse, one-shot best-effort fetch (a company's own homepage isn't
  rate-limiting anyone the way an ATS API might, per this task's project-conventions note), not a
  retried/backed-off call like pipeline.jobs.ats.fetch_json(): any request exception is treated as
  "no result", the same as a company with no reachable homepage falling through the ladder. It
  scans raw href/src attribute values with a simple regex, not a DOM parser (a coarse fingerprint
  doesn't need one), and re-uses pipeline.jobs.ats.match_any_url() against every extracted URL
  instead of hand-rolling a separate list of known ATS hostnames, so the set of hosts recognized
  here never drifts from what match_any_url() itself already knows. It follows at most one
  "careers"/"job" link one hop deep when the homepage itself has no ATS link.
- resolve_company() threads fingerprint_html()'s internal careers-page URL (when it found one,
  even on an otherwise-missed fingerprint) into the step-4 JSON-LD fallback, instead of re-deriving
  a guessed URL, via the private _fingerprint() both public functions share — fingerprint_html()
  is the SCOPE_FILES-mandated public entrypoint over the same logic.
"""
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests

from pipeline.db import JobCompany
from pipeline.jobs import slug_candidates
from pipeline.jobs.ats import get_adapter, match_any_url

_HTTP_TIMEOUT = 10
_USER_AGENT = "SignalSlate JobsCollector/1.0 (+https://github.com/lasswellt/signalslate)"

# The only ATS kinds whose probe() accepts a bare guessed slug (research doc "Resolution steps").
# workday needs tenant|wdN|site, bamboohr/recruitee/personio need more than a slug, jsonld has no
# slug concept (its board_id is a careers-page URL).
_SLUG_PROBE_KINDS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "rippling")
_MAX_SLUG_PROBES = 15

_SLUG_PROBE_CONFIDENCE_NAME_CONFIRMED = 0.9
_SLUG_PROBE_CONFIDENCE_UNCONFIRMED = 0.7

_HTML_FINGERPRINT_CONFIDENCE = 0.85

_ATTR_URL_RE = re.compile(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_CAREERS_KEYWORDS = ("career", "job")


@dataclass(frozen=True)
class ResolveResult:
    """One resolver-ladder hit: which ATS, which board, how it was found, and how sure we are."""
    ats_kind: str
    board_id: str
    resolved_by: str  # pattern | slug_probe | html | jsonld
    confidence: float


def _normalize_for_match(value: str) -> str:
    """Lower-cases value and folds non-alnum runs to a single space, for a loose name comparison."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _names_loosely_match(returned_name: str, company_name: str) -> bool:
    """Whether returned_name (an ATS's own company_name field) plausibly refers to company_name:
    a case-insensitive substring match either way, or at least one shared token, so a probe hit
    naming an unrelated company (the "generic slug exists on someone else's board" false-positive
    risk) is rejected rather than trusted."""
    a = _normalize_for_match(returned_name)
    b = _normalize_for_match(company_name)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return bool(set(a.split()) & set(b.split()))


def probe_slugs(company: JobCompany) -> Optional[ResolveResult]:
    """
    Tries guessed board slugs against the ATS kinds whose probe() accepts a bare slug.

    Args:
        company: The company to guess slugs for (uses its name and, if set, domain).

    Returns:
        A ResolveResult(resolved_by="slug_probe") for the first accepted hit — confidence 0.9 when
        the adapter's own returned company_name loosely matches company.name, 0.7 when the adapter
        returns no company_name to check at all — or None once every candidate/kind combination
        (capped at _MAX_SLUG_PROBES) has been tried without an accepted hit. A hit whose returned
        company_name does NOT loosely match company.name is rejected (not returned, not counted as
        a miss that stops the search) as a likely false positive.
    """
    candidates = slug_candidates(company.name, company.domain)
    if not candidates:
        return None

    attempts = 0
    for slug in candidates:
        for kind in _SLUG_PROBE_KINDS:
            if attempts >= _MAX_SLUG_PROBES:
                return None
            attempts += 1
            adapter = get_adapter(kind)
            try:
                info = adapter.probe(slug)
            except RuntimeError:
                # Transient network/5xx flake (fetch_json "unavailable") — not a verdict on this
                # slug/kind guess, so keep trying the rest rather than aborting the whole ladder.
                continue
            if info is None:
                continue
            if info.company_name:
                if not _names_loosely_match(info.company_name, company.name):
                    continue
                confidence = _SLUG_PROBE_CONFIDENCE_NAME_CONFIRMED
            else:
                confidence = _SLUG_PROBE_CONFIDENCE_UNCONFIRMED
            return ResolveResult(
                ats_kind=kind,
                board_id=info.board_id,
                resolved_by="slug_probe",
                confidence=confidence,
            )
    return None


def _fetch_html(url: str) -> Optional[str]:
    """url's response body, or None on any request exception or non-2xx status — best-effort, a
    company's own homepage not being reachable just means this ladder step falls through."""
    try:
        response = requests.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT
        )
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    return response.text


def _find_ats_match(html: str, base_url: str) -> Optional[tuple[str, str]]:
    """The first (kind, board_id) match_any_url() finds among html's href/src attribute values,
    resolved against base_url, or None when no attribute URL matches a known ATS pattern."""
    for raw_url in _ATTR_URL_RE.findall(html):
        match = match_any_url(urljoin(base_url, raw_url))
        if match:
            return match
    return None


def _find_careers_link(html: str, base_url: str) -> Optional[str]:
    """The first anchor (absolute URL) whose href or visible text mentions "career"/"job"
    (case-insensitive), or None when no such anchor is present. One hop only — resolve_company/
    fingerprint_html do not recurse past this."""
    for href, text in _ANCHOR_RE.findall(html):
        haystack = f"{href} {text}".lower()
        if any(keyword in haystack for keyword in _CAREERS_KEYWORDS):
            return urljoin(base_url, href)
    return None


def _fingerprint(company: JobCompany) -> tuple[Optional[ResolveResult], Optional[str]]:
    """Shared implementation behind fingerprint_html() and resolve_company()'s step-4 threading.

    Returns:
        (result, careers_url): result is a ResolveResult(resolved_by="html") on a hit, else None.
        careers_url is the careers-page URL fetched along the way (the homepage itself if no
        careers link was found/followed, or the followed careers link when one was), or None when
        company.domain is unset — resolve_company reuses it for the JSON-LD fallback instead of
        re-guessing a URL.
    """
    if not company.domain:
        return None, None

    homepage_url = f"https://{company.domain}/"
    homepage_html = _fetch_html(homepage_url)
    if homepage_html is None:
        return None, None

    match = _find_ats_match(homepage_html, homepage_url)
    if match:
        kind, board_id = match
        return (
            ResolveResult(
                ats_kind=kind,
                board_id=board_id,
                resolved_by="html",
                confidence=_HTML_FINGERPRINT_CONFIDENCE,
            ),
            homepage_url,
        )

    careers_url = _find_careers_link(homepage_html, homepage_url)
    if not careers_url:
        return None, homepage_url

    careers_html = _fetch_html(careers_url)
    if careers_html is None:
        return None, careers_url

    match = _find_ats_match(careers_html, careers_url)
    if match:
        kind, board_id = match
        return (
            ResolveResult(
                ats_kind=kind,
                board_id=board_id,
                resolved_by="html",
                confidence=_HTML_FINGERPRINT_CONFIDENCE,
            ),
            careers_url,
        )
    return None, careers_url


def fingerprint_html(company: JobCompany) -> Optional[ResolveResult]:
    """
    Fetches company's homepage (and, if needed, one linked careers page) and scans for a known
    ATS's URL pattern.

    Args:
        company: The company to fingerprint; only runs when company.domain is set.

    Returns:
        A ResolveResult(resolved_by="html", confidence=0.85) when either the homepage or one
        followed "career"/"job" link contains a URL matching a known ATS's pattern
        (pipeline.jobs.ats.match_any_url()); None when company.domain is unset, the homepage isn't
        reachable, or no ATS URL is found on either page.
    """
    result, _ = _fingerprint(company)
    return result


def resolve_company(
    company: JobCompany, known_urls: Optional[list[str]] = None
) -> Optional[ResolveResult]:
    """
    Runs the resolver ladder for company, first hit wins.

    Args:
        company: The company to resolve a JobBoard for.
        known_urls: Candidate URLs already associated with company (e.g. a seed importer's or a
            prior careers-page link), tried against known ATS URL patterns before any network call.

    Returns:
        The first hit among, in order: (1) a known_urls entry matching a known ATS URL pattern
        (resolved_by="pattern", confidence=1.0); (2) probe_slugs(company); (3)
        fingerprint_html(company); (4) the generic JSON-LD adapter probed against the careers URL
        fingerprint_html found along the way, or a guessed "https://{domain}/careers" URL when
        fingerprint_html found none. None when every step misses (T-012's LLM research step and
        T-015's wiring handle that case, not this function).
    """
    for url in known_urls or []:
        match = match_any_url(url)
        if match:
            kind, board_id = match
            return ResolveResult(
                ats_kind=kind, board_id=board_id, resolved_by="pattern", confidence=1.0
            )

    slug_result = probe_slugs(company)
    if slug_result:
        return slug_result

    html_result, careers_url = _fingerprint(company)
    if html_result:
        return html_result

    if not company.domain:
        return None
    jsonld_url = careers_url or f"https://{company.domain}/careers"
    jsonld_adapter = get_adapter("jsonld")
    try:
        info = jsonld_adapter.probe(jsonld_url)
    except RuntimeError:
        return None
    if info is None:
        return None
    return ResolveResult(
        ats_kind="jsonld",
        board_id=info.board_id,
        resolved_by="jsonld",
        confidence=info.confidence,
    )
