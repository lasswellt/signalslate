"""
Subdomain and archived-URL intel for the domains subsystem (Research Finding 5 / Q4).

Design decisions:

- subdomains() (crt.sh certificate-transparency JSON) and archived_urls() (the Wayback Machine's
  CDX API) are on-demand only (research doc: "On-demand only, not in daily refresh"), so neither
  persists anything; every call fetches fresh.
- crt.sh (5 req/min per IP, frequent 502s per the research doc) and the Wayback CDX API both get
  exactly one retry after a fixed backoff before giving up: an on-demand lookup a human is waiting
  on does not get the patient multi-minute retry budget a bulk job would, and the research doc
  already calls both sources "best effort".
- Like pipeline.domains.dns and pipeline.domains.rdap, nothing here raises for an expected failure:
  every outcome is a typed status ("ok" | "unavailable" | "error") on a frozen result.
  "unavailable" covers a transient failure (timeout, connection error, or a retryable 429/5xx) that
  did not recover after the one retry — the crt.sh/Wayback flakiness the research doc anticipates.
  "error" covers anything unexpected: a non-retryable HTTP status or a response body that is not
  the shape either API documents. Either way only the exception CLASS name or an "HTTP<code>"
  marker is recorded (pipeline.redact / pipeline.runner convention — never a message, which can
  carry request/response detail).
- _sleep is a module-level indirection over time.sleep (pipeline.collectors.gmail's pattern), so
  tests replace it with a list-appending fake instead of waiting out the real backoff.
- crt.sh certificate names include wildcards ("*.example.com") and, for a busy CA, occasional
  entries unrelated to the queried domain that happen to share a SAN list; wildcards are stripped
  to their bare name and every kept name (from either source) must equal `name` or end with
  "." + name, so nothing outside the domain's own subdomain tree leaks into the result.
"""
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import requests

_log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_MAX_ATTEMPTS = 2  # one retry, per the research doc's crt.sh guidance
_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_MAX_SUBDOMAINS = 500
_WAYBACK_LIMIT = 200

_CRTSH_URL = "https://crt.sh/"
_WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"

# Indirection so tests neither wait nor depend on real timing.
_sleep = time.sleep


@dataclass(frozen=True)
class SubdomainsResult:
    """crt.sh's certificate-transparency view of a domain's subdomains. status="ok" is the only
    status where names carries real data; every other status leaves it empty and, unless the
    source was merely unreachable, records the failure in error."""
    status: str  # "ok" | "unavailable" | "error"
    names: tuple[str, ...]
    error: Optional[str]


@dataclass(frozen=True)
class ArchivedUrlsResult:
    """The Wayback Machine's CDX view of URLs archived under a domain. status="ok" is the only
    status where urls carries real data."""
    status: str  # "ok" | "unavailable" | "error"
    urls: tuple[str, ...]
    error: Optional[str]


def _strip_wildcard(candidate: str) -> str:
    """crt.sh SAN entries are sometimes a wildcard ("*.example.com"); this returns the bare name
    underneath, since the wildcard marker itself is not a usable subdomain."""
    return candidate[2:] if candidate.startswith("*.") else candidate


def _owns(candidate: str, name: str) -> bool:
    """True when candidate is name itself or one of its subdomains."""
    return candidate == name or candidate.endswith("." + name)


def _host_of(url: str) -> Optional[str]:
    """The lower-case hostname of url, or None when it cannot be parsed as one."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    return parsed.hostname.lower() if parsed.hostname else None


def _fetch_json(url: str, params: dict) -> tuple[Any, str, Optional[str]]:
    """
    GETs url with one retry after a fixed backoff on a transient failure.

    Args:
        url: The endpoint to GET.
        params: Query parameters, passed through to requests.get.

    Returns:
        (data, status, error):
          - status="ok": data is the response's decoded JSON body; error is None.
          - status="unavailable": every attempt failed transiently (a timeout, connection error,
            or a 429/5xx status); data is None and error is the last exception class name or an
            "HTTP<code>" marker.
          - status="error": the request failed for a non-transient reason (a non-retryable HTTP
            status) or the response body was not valid JSON; data is None and error names the
            failure.
    """
    last_error: Optional[str] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            if attempt < _MAX_ATTEMPTS - 1:
                _sleep(_BACKOFF_SECONDS)
            continue

        if response.status_code in _RETRYABLE_STATUS:
            last_error = f"HTTP{response.status_code}"
            if attempt < _MAX_ATTEMPTS - 1:
                _sleep(_BACKOFF_SECONDS)
            continue

        if response.status_code >= 400:
            return None, "error", f"HTTP{response.status_code}"

        try:
            return response.json(), "ok", None
        except ValueError as exc:
            return None, "error", type(exc).__name__

    return None, "unavailable", last_error


def subdomains(name: str) -> SubdomainsResult:
    """
    Subdomains crt.sh's certificate-transparency log JSON has recorded for name, best effort.

    Args:
        name: A domain as normalize_domain() returns it (lower-case punycode).

    Returns:
        A SubdomainsResult. names is deduplicated, wildcard-stripped, capped at
        _MAX_SUBDOMAINS entries and limited to name itself or one of its subdomains. Never raises;
        a crt.sh outage or malformed response comes back as a typed status instead.
    """
    data, status, error = _fetch_json(_CRTSH_URL, {"q": f"%.{name}", "output": "json"})
    if status != "ok":
        _log.warning("crt.sh lookup failed for %s: %s", name, error)
        return SubdomainsResult(status=status, names=(), error=error)

    if not isinstance(data, list):
        _log.warning("crt.sh lookup for %s returned an unexpected shape", name)
        return SubdomainsResult(status="error", names=(), error="UnexpectedResponseShape")

    found: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("name_value")
        if not isinstance(raw, str):
            continue
        for candidate in raw.split("\n"):
            stripped = _strip_wildcard(candidate.strip().lower())
            if stripped and _owns(stripped, name):
                found.add(stripped)

    return SubdomainsResult(status="ok", names=tuple(sorted(found)[:_MAX_SUBDOMAINS]), error=None)


def archived_urls(name: str) -> ArchivedUrlsResult:
    """
    URLs the Wayback Machine's CDX API has archived under name, best effort.

    Args:
        name: A domain as normalize_domain() returns it (lower-case punycode).

    Returns:
        An ArchivedUrlsResult. urls is deduplicated (collapse=urlkey on the CDX query already
        collapses consecutive captures of the same URL; the dedupe here also guards a repeat
        appearing non-consecutively), capped at _WAYBACK_LIMIT entries and limited to URLs whose
        host is name itself or one of its subdomains. Never raises.
    """
    params = {
        "url": f"{name}/*",
        "output": "json",
        "collapse": "urlkey",
        "limit": str(_WAYBACK_LIMIT),
    }
    data, status, error = _fetch_json(_WAYBACK_CDX_URL, params)
    if status != "ok":
        _log.warning("wayback cdx lookup failed for %s: %s", name, error)
        return ArchivedUrlsResult(status=status, urls=(), error=error)

    if not isinstance(data, list):
        _log.warning("wayback cdx lookup for %s returned an unexpected shape", name)
        return ArchivedUrlsResult(status="error", urls=(), error="UnexpectedResponseShape")
    if not data:
        return ArchivedUrlsResult(status="ok", urls=(), error=None)

    header = data[0]
    if not isinstance(header, list) or "original" not in header:
        _log.warning("wayback cdx lookup for %s returned an unexpected shape", name)
        return ArchivedUrlsResult(status="error", urls=(), error="UnexpectedResponseShape")
    original_index = header.index("original")

    seen: set[str] = set()
    found: list[str] = []
    for row in data[1:]:
        if len(found) >= _WAYBACK_LIMIT:
            break
        if not isinstance(row, list) or len(row) <= original_index:
            continue
        url = row[original_index]
        if not isinstance(url, str) or url in seen:
            continue
        host = _host_of(url)
        if host is None or not _owns(host, name):
            continue
        seen.add(url)
        found.append(url)

    return ArchivedUrlsResult(status="ok", urls=tuple(found), error=None)
