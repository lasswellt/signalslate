"""
ATS adapter protocol, shared value objects and lazy registry for pipeline/jobs/ats/.

Design decisions:

- AtsAdapter is a typing.Protocol, not a base class: each pipeline.jobs.ats.<kind> module (T-005..
  T-010) defines a module-level `adapter` object satisfying this shape without importing anything
  from this package, so this file never becomes a base class every adapter must subclass.
- get_adapter()/ADAPTER_KINDS keep the registry lazy (research doc "Implementation Sketch" Step 2):
  importing pipeline.jobs.ats must not import all 11 adapter modules, most of which don't exist
  until T-005..T-010 land, and even once every one exists there's no reason to pay for a module a
  given run never touches.
- match_any_url() tolerates a not-yet-implemented kind (ModuleNotFoundError) instead of crashing:
  only greenhouse/lever/etc. land per-task, so discover.py (a later step) needs to keep working
  against whichever adapters currently exist rather than erroring until all 11 are in.
- fetch_json() mirrors pipeline/domains/intel.py's _fetch_json(): module-level _sleep indirection
  so tests replace it with a list-appending fake instead of waiting, _MAX_ATTEMPTS/
  _BACKOFF_SECONDS/_RETRYABLE_STATUS constants, and a typed result rather than raising for an
  expected failure (a private/unknown board 404s constantly; that's not exceptional). Unlike
  intel.py this also honors a Retry-After header — ATS APIs research doc Q6 explicitly calls for
  it ("poll politely... honor 429/Retry-After") — parsed as delta-seconds and used as the sleep
  instead of the fixed backoff when present, since the server is telling us exactly how long to
  wait rather than us guessing.
"""
import importlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import requests

_log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 5.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_USER_AGENT = "SignalSlate JobsCollector/1.0 (+https://github.com/lasswellt/signalslate)"

# Indirection so tests neither wait nor depend on real timing (pipeline/domains/intel.py's pattern).
_sleep = time.sleep

# The 11 ATS kinds the research doc's resolver/adapter ladder targets. Order is not significant
# here (match_any_url tries all of them); each kind's own module is what may or may not exist yet.
ADAPTER_KINDS = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "workday",
    "smartrecruiters",
    "rippling",
    "bamboohr",
    "recruitee",
    "personio",
    "jsonld",
)


@dataclass(frozen=True)
class BoardInfo:
    """What resolve.py needs to confirm/store a JobBoard once probe() finds one. confidence is the
    adapter's own best guess (e.g. 1.0 for an exact API-confirmed match); resolve.py may combine it
    with how the board was found (resolved_by) rather than store it verbatim."""
    board_id: str
    company_name: Optional[str] = None
    confidence: float = 1.0


@dataclass(frozen=True)
class RawPosting:
    """One posting as an adapter's list_postings() returns it, before the inventory step upserts it
    into JobPosting. Fields mirror JobPosting's own columns (pipeline/db.py) plus description,
    which JobPosting doesn't persist but content_hash() (pipeline/jobs) needs for change detection.
    Left un-normalized here; normalization happens once, at upsert time, not per-adapter."""
    external_id: str
    title: str
    url: str
    description: str = ""
    location: Optional[str] = None
    remote: Optional[bool] = None
    comp_text: Optional[str] = None
    posted_at: Optional[str] = None


class AtsAdapter(Protocol):
    """The shape every pipeline.jobs.ats.<kind> module's `adapter` object satisfies."""

    kind: str
    apply_mode: str  # hosted_form | account_per_tenant

    def match_url(self, url: str) -> Optional[str]:
        """The board_id url resolves to on this ATS, or None if url isn't one of this ATS's URLs."""
        ...

    def probe(self, slug: str) -> Optional[BoardInfo]:
        """BoardInfo for slug on this ATS, or None when the ATS reports the board doesn't exist
        (e.g. a 404) — a routine "wrong guess", not an error."""
        ...

    def list_postings(self, board_id: str) -> list[RawPosting]:
        """Every open posting board_id currently lists on this ATS."""
        ...


def get_adapter(kind: str) -> AtsAdapter:
    """
    The adapter object for kind, importing its module on first use.

    Args:
        kind: One of ADAPTER_KINDS.

    Returns:
        pipeline.jobs.ats.<kind>'s module-level `adapter`.

    Raises:
        ValueError: kind is not one of ADAPTER_KINDS.
        ModuleNotFoundError: kind is a valid ADAPTER_KINDS entry but its module hasn't landed yet.
    """
    if kind not in ADAPTER_KINDS:
        raise ValueError(f"unknown ATS kind: {kind!r}")
    module = importlib.import_module(f"pipeline.jobs.ats.{kind}")
    return module.adapter


def match_any_url(url: str) -> Optional[tuple[str, str]]:
    """
    Tries every ADAPTER_KINDS entry's match_url() against url, first match wins.

    Args:
        url: A URL to test, e.g. a company's careers-page link.

    Returns:
        (kind, board_id) for the first implemented adapter whose match_url(url) returns a
        board_id, or None if none match. A kind whose module doesn't exist yet is skipped, not an
        error, since T-005..T-010 land the 11 adapters one at a time.
    """
    for kind in ADAPTER_KINDS:
        try:
            adapter = get_adapter(kind)
        except ModuleNotFoundError:
            continue
        board_id = adapter.match_url(url)
        if board_id is not None:
            return kind, board_id
    return None


def _retry_after_seconds(response: "requests.Response") -> Optional[float]:
    """The Retry-After header's value in seconds, or None when absent/unparseable. Only the
    delta-seconds form is handled — every ATS this collector polls sends that, not an HTTP-date —
    so an HTTP-date Retry-After falls back to the fixed backoff instead of being parsed as one."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def fetch_json(
    url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None
) -> tuple[Any, str, Optional[str]]:
    """
    GETs url with up to _MAX_ATTEMPTS tries, retrying a transient failure with backoff.

    Args:
        url: The endpoint to GET.
        params: Query parameters, passed through to requests.get.
        headers: Extra headers merged over the descriptive User-Agent every request already sends.

    Returns:
        (data, status, error), matching pipeline/domains/intel.py's _fetch_json() contract:
          - status="ok": data is the response's decoded JSON body; error is None.
          - status="unavailable": every attempt failed transiently (a timeout, connection error, or
            a retryable 429/5xx status); data is None and error is the last exception class name or
            an "HTTP<code>" marker. A retryable response carrying a Retry-After header sleeps that
            long instead of the fixed backoff before the next attempt.
          - status="error": the request failed for a non-transient reason (a non-retryable HTTP
            status) or the response body was not valid JSON; data is None and error names it.
    """
    request_headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    last_error: Optional[str] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                url, params=params, headers=request_headers, timeout=_HTTP_TIMEOUT
            )
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            if attempt < _MAX_ATTEMPTS - 1:
                _sleep(_BACKOFF_SECONDS)
            continue

        if response.status_code in _RETRYABLE_STATUS:
            last_error = f"HTTP{response.status_code}"
            if attempt < _MAX_ATTEMPTS - 1:
                retry_after = _retry_after_seconds(response)
                _sleep(retry_after if retry_after is not None else _BACKOFF_SECONDS)
            continue

        if response.status_code >= 400:
            return None, "error", f"HTTP{response.status_code}"

        try:
            return response.json(), "ok", None
        except ValueError as exc:
            return None, "error", type(exc).__name__

    return None, "unavailable", last_error
