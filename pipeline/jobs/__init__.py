"""
Jobs-collector shared primitives: the module's config, ATS slug-guessing, dedup-comparison text
normalization, and the posting change-detection hash.

Design decisions:

- jobs_settings() reads through pipeline.health.env(), the same overlay-aware config surface
  domain_settings() uses (project convention), instead of os.environ directly. Unlike domain
  purchasing, nothing here spends money, so a malformed numeric env var fails closed to its safe
  default (logged, not raised) rather than needing an all-or-nothing kill switch.
- slug_candidates() derives at most 3 ordered, deduped ATS board-slug guesses from a company name
  and its domain (research doc: "derive candidates from name/domain... At most 5 platforms x 3
  slugs = 15 cheap GETs per company"). Order matters to callers: the caller probes candidates in
  the returned order and stops at the first hit, so cheapest/most-likely guesses come first.
- normalize_title()/normalize_city() exist for the cross-source dedup key
  (`norm(company)::norm(title)::norm(city)`, research doc "Dedup / change detection"): two postings
  that differ only in casing, whitespace or punctuation must compare equal, so folding out that
  noise up front is cheaper and less error-prone than teaching every comparison site the same rules.
- content_hash() mirrors pipeline/domains/snapshot.py's _data_hash() pattern (sha256 over
  json.dumps(fields, sort_keys=True)) but with two ATS-specific exclusions so a re-fetch of an
  unchanged posting does not produce a spurious new hash (pipeline/db.py JobPosting.content_hash
  docstring): description whitespace (ATS rewrite it cosmetically on every poll) and apply_url
  tracking query params (utm_* and similar, appended/reordered per fetch, carry no job-content
  signal).
"""
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pipeline import health

_log = logging.getLogger(__name__)

DEFAULT_REFRESH_CRON = "0 6 * * *"
DEFAULT_SCORE_THRESHOLD = 0.6
DEFAULT_MISSED_POLLS_TO_CLOSE = 3
DEFAULT_MAX_LLM_RESOLVES_PER_RUN = 10
DEFAULT_MAX_SCORE_PER_RUN = 50

_MAX_SLUG_CANDIDATES = 3
_COMPANY_SUFFIX_TOKENS = frozenset({
    "inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "group",
    "holdings",
})
# Appended/reordered per fetch by the ATS or an email/link redirector; never part of a job's
# actual content, so they must not perturb content_hash().
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = frozenset({
    "gclid", "fbclid", "mc_cid", "mc_eid", "gh_src", "lever-source", "ref", "igshid",
})


@dataclass(frozen=True)
class JobsSettings:
    """The jobs-collector's config, resolved once per call so every reader agrees within a run."""
    refresh_cron: str
    score_threshold: float
    missed_polls_to_close: int
    max_llm_resolves_per_run: int
    max_score_per_run: int


def _float_env(env: dict, key: str, default: float) -> float:
    """The parsed float, or default (logged) when unset or not a valid number."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _log.warning("jobs_settings: %s is not a valid number, using default", key)
        return default


def _int_env(env: dict, key: str, default: int) -> int:
    """The parsed int, or default (logged) when unset or not a valid integer."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("jobs_settings: %s is not a valid integer, using default", key)
        return default


def jobs_settings() -> JobsSettings:
    """
    Reads JOBS_* config through pipeline.health.env().

    A malformed JOBS_SCORE_THRESHOLD, JOBS_MISSED_POLLS_TO_CLOSE, JOBS_MAX_LLM_RESOLVES_PER_RUN or
    JOBS_MAX_SCORE_PER_RUN falls back to its documented default (logged) rather than raising:
    nothing this config gates is a spend operation, so failing open to a safe default is enough.

    Returns:
        JobsSettings with every field defaulted, so callers never see a missing key.
    """
    env = health.env()
    refresh_cron = (env.get("JOBS_REFRESH_CRON") or "").strip() or DEFAULT_REFRESH_CRON

    return JobsSettings(
        refresh_cron=refresh_cron,
        score_threshold=_float_env(env, "JOBS_SCORE_THRESHOLD", DEFAULT_SCORE_THRESHOLD),
        missed_polls_to_close=_int_env(
            env, "JOBS_MISSED_POLLS_TO_CLOSE", DEFAULT_MISSED_POLLS_TO_CLOSE
        ),
        max_llm_resolves_per_run=_int_env(
            env, "JOBS_MAX_LLM_RESOLVES_PER_RUN", DEFAULT_MAX_LLM_RESOLVES_PER_RUN
        ),
        max_score_per_run=_int_env(env, "JOBS_MAX_SCORE_PER_RUN", DEFAULT_MAX_SCORE_PER_RUN),
    )


def _alnum_tokens(value: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9]+", value.lower())


def _stripped_name_slug(name: str) -> str:
    """The company name's alnum tokens, minus any trailing legal-suffix tokens (Inc/LLC/Corp/...)."""
    tokens = _alnum_tokens(name)
    while tokens and tokens[-1] in _COMPANY_SUFFIX_TOKENS:
        tokens.pop()
    return "".join(tokens)


def _full_name_slug(name: str) -> str:
    """The company name's alnum tokens concatenated as-is, suffix included."""
    return "".join(_alnum_tokens(name))


def _domain_sld(domain: str) -> str:
    """The domain's second-level-domain label (e.g. "acme.com" -> "acme"), alnum-only."""
    labels = [label for label in domain.strip().lower().split(".") if label]
    if not labels:
        return ""
    sld = labels[-2] if len(labels) >= 2 else labels[0]
    return re.sub(r"[^a-z0-9]", "", sld)


def slug_candidates(name: str, domain: Optional[str] = None) -> list[str]:
    """
    Ordered, deduped ATS board-slug guesses for a company, cheapest/most-likely first.

    Args:
        name: The company's display name, e.g. "Acme Inc".
        domain: The company's website domain, e.g. "acme.com"; optional.

    Returns:
        Up to 3 lower-case alnum-only candidates: the name with a trailing legal suffix
        (Inc/LLC/Corp/...) stripped, the name's full concatenation, and the domain's
        second-level-domain label — in that order, deduped, e.g. "Acme Inc" + "acme.com" ->
        ["acme", "acmeinc"].
    """
    candidates: list[str] = []
    if name:
        candidates.append(_stripped_name_slug(name))
        candidates.append(_full_name_slug(name))
    if domain:
        candidates.append(_domain_sld(domain))

    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped[:_MAX_SLUG_CANDIDATES]


def _normalize_text(value: Optional[str]) -> str:
    """Lower-cases value and folds every run of non-alnum characters (whitespace or punctuation)
    to a single space, trimmed — so casing/whitespace/punctuation variance never breaks equality."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def normalize_title(title: Optional[str]) -> str:
    """Normalizes a job title for dedup comparison (`norm(title)` in the cross-source dedup key)."""
    return _normalize_text(title)


def normalize_city(city: Optional[str]) -> str:
    """Normalizes a city/location string for dedup comparison (`norm(city)` in the cross-source
    dedup key). An empty result acts as a wildcard per the dedup key's own rule."""
    return _normalize_text(city)


def _normalize_description(description: Optional[str]) -> str:
    """Collapses whitespace runs to a single space and trims, without touching casing or
    punctuation: content_hash only needs to ignore an ATS's cosmetic re-wrapping of the same text."""
    if not description:
        return ""
    return re.sub(r"\s+", " ", description).strip()


def _strip_tracking_params(url: Optional[str]) -> str:
    """Drops utm_* and other known tracking query params from url, keeping the rest (sorted, so
    param reordering across fetches never perturbs the hash) and dropping any fragment."""
    if not url:
        return ""
    parts = urlsplit(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
        and key.lower() not in _TRACKING_PARAM_NAMES
    ]
    query = urlencode(sorted(kept))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def content_hash(
    title: str,
    description: str = "",
    *,
    location: Optional[str] = None,
    remote: Optional[bool] = None,
    comp_text: Optional[str] = None,
    apply_url: str = "",
) -> str:
    """
    A stable sha256 fingerprint of a posting's content, for change detection across polls.

    Args:
        title: The posting's title, as the ATS returns it.
        description: The posting's full description text; whitespace-only differences (an ATS's
            cosmetic re-wrapping) are excluded.
        location: The posting's location text, if any.
        remote: Whether the posting is remote, if the ATS reports it.
        comp_text: The posting's compensation text, if any.
        apply_url: The posting's apply URL; tracking query params (utm_* and similar) are excluded
            so a link re-fetched with different tracking noise still hashes the same.

    Returns:
        A hex sha256 digest over the normalized fields, stable across re-fetches of an
        unchanged posting.
    """
    fields = {
        "title": (title or "").strip(),
        "description": _normalize_description(description),
        "location": (location or "").strip() or None,
        "remote": remote,
        "comp_text": (comp_text or "").strip() or None,
        "apply_url": _strip_tracking_params(apply_url),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
