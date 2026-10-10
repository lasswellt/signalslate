"""
Carrier adapter foundation: result types, typed errors, OAuth token cache, rate limiter, HTTP helper.

Design decisions:

- TrackResult/TrackEvent are frozen dataclasses with the attributes inventory.CarrierResultLike
  needs, so apply_carrier_result() consumes them directly. Statuses are always members of
  pipeline.packages.STATUSES; normalise_status() maps anything unrecognised to "unknown".
- Failures are typed (auth, not found, rate limited, unavailable) so the poller can decide per
  class: auth errors disable a carrier, not-found is per package, the rest are retried next run.
- request_json retries 429/5xx up to MAX_ATTEMPTS with the truncated exponential backoff shape of
  collectors/gmail.py and honours Retry-After. Sleep and randomness are module indirections so
  tests neither wait nor depend on chance.
- Tokens are cached until expires_in minus 60s, with an injectable clock. The client secret is
  repr-hidden and never logged; only exception class names and HTTP statuses are logged.
- RateLimiter enforces a per-carrier minimum interval (sleeps) and a sliding hourly budget
  (raises CarrierRateLimited rather than blocking a scheduler thread for up to an hour).
- adapters() imports ups/usps lazily so this module has no dependency on them. ups.py registers
  as "ups" and usps.py as "usps" by exposing a module-level `adapter()` factory returning a
  CarrierAdapter; unconfigured adapters are omitted.
"""
import importlib
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

import requests

from pipeline.packages import STATUSES

_log = logging.getLogger(__name__)

TIMEOUT = 15
MAX_ATTEMPTS = 4
MAX_BACKOFF = 32
RETRY_STATUS = {429, 500, 502, 503, 504}
TOKEN_SKEW_SECONDS = 60
DEFAULT_TOKEN_TTL = 300

# Indirections so tests neither wait nor depend on real randomness.
_sleep = time.sleep
_random = random.random


class CarrierError(RuntimeError):
    """Base class for every carrier failure."""


class CarrierAuthError(CarrierError):
    """Credentials rejected or not authorised for this API (HTTP 401/403)."""


class CarrierNotFound(CarrierError):
    """The carrier does not know this tracking number (HTTP 404)."""


class CarrierRateLimited(CarrierError):
    """Carrier (or our local budget) says to slow down."""

    def __init__(self, message: str = "rate limited", retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CarrierUnavailable(CarrierError):
    """Carrier 5xx, timeout or network failure."""


@dataclass(frozen=True)
class TrackEvent:
    """One carrier scan event."""
    occurred_at: datetime
    status: str
    description: str
    location: Optional[str] = None


@dataclass(frozen=True)
class TrackResult:
    """Carrier view of one tracking number; satisfies inventory.CarrierResultLike."""
    tracking_number: str
    status: str
    events: tuple[TrackEvent, ...] = ()
    eta: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


class CarrierAdapter(Protocol):
    """A carrier tracking client."""
    code: str

    def configured(self) -> bool:
        """Whether credentials are present."""
        ...

    def track(self, tracking_number: str) -> TrackResult:
        """Fetch current status; raises a CarrierError subclass on failure."""
        ...


def normalise_status(value: Optional[str]) -> str:
    """
    Map a carrier status string to the package status enum.

    Args:
        value: Carrier-specific status text or code; may already be an enum member.

    Returns:
        A member of pipeline.packages.STATUSES; "unknown" when unrecognised.
    """
    if not value:
        return "unknown"
    text = value.strip().lower().replace("-", "_").replace(" ", "_")
    if text in STATUSES:
        return text
    if "out_for_delivery" in text:
        return "out_for_delivery"
    if "return" in text:
        return "returned"
    if "deliver" in text:
        if any(w in text for w in ("attempt", "fail", "not_deliver", "undeliver", "delay")):
            return "exception"
        return "delivered"
    if any(w in text for w in ("exception", "delay", "hold", "damage", "refused", "alert")):
        return "exception"
    if any(w in text for w in ("transit", "departed", "arrived", "accepted", "processed",
                               "picked_up", "origin_scan", "in_process")):
        return "in_transit"
    if any(w in text for w in ("label", "pre_shipment", "shipment_ready", "manifest",
                               "order_processed", "created")):
        return "pending"
    return "unknown"


def _backoff(attempt: int) -> float:
    """Truncated exponential backoff: min(2^n + random fraction of a second, MAX_BACKOFF)."""
    return min(2**attempt + _random(), MAX_BACKOFF)


def _retry_after(resp: requests.Response) -> Optional[float]:
    raw = resp.headers.get("Retry-After") if resp.headers else None
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


class OAuthClientCredentials:
    """OAuth2 client-credentials grant with a bearer cache."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, *,
                 scope: Optional[str] = None, basic_auth: bool = False,
                 clock: Callable[[], float] = time.time) -> None:
        """
        Args:
            token_url: Token endpoint.
            client_id: OAuth client id.
            client_secret: OAuth client secret (never shown in repr).
            scope: Optional scope string.
            basic_auth: True sends HTTP Basic (UPS); False sends client_id/secret as form fields (USPS).
            clock: Epoch-seconds source, injectable for tests.
        """
        self.token_url = token_url
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self.basic_auth = basic_auth
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return (f"OAuthClientCredentials(token_url={self.token_url!r}, "
                f"client_id={self.client_id!r}, basic_auth={self.basic_auth})")

    def get_token(self) -> str:
        """
        Return a valid bearer token, fetching a new one when the cache is within 60s of expiry.

        Returns:
            The access token.

        Raises:
            CarrierAuthError: Credentials rejected (401/403) or no token in the response.
            CarrierUnavailable: Network failure, timeout or 5xx.
        """
        with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            data = {"grant_type": "client_credentials"}
            if self.scope:
                data["scope"] = self.scope
            kwargs: dict[str, Any] = {"data": data, "timeout": TIMEOUT}
            if self.basic_auth:
                kwargs["auth"] = (self.client_id, self._client_secret)
            else:
                data["client_id"] = self.client_id
                data["client_secret"] = self._client_secret
            try:
                resp = requests.post(self.token_url, **kwargs)
            except requests.RequestException as exc:
                raise CarrierUnavailable(f"token endpoint: {type(exc).__name__}") from exc
            if resp.status_code in (400, 401, 403):
                raise CarrierAuthError(f"token endpoint rejected credentials (HTTP {resp.status_code})")
            if resp.status_code >= 400:
                raise CarrierUnavailable(f"token endpoint HTTP {resp.status_code}")
            try:
                body = resp.json()
                token = body["access_token"]
                ttl = float(body.get("expires_in", DEFAULT_TOKEN_TTL))
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                raise CarrierAuthError("token endpoint returned no usable token") from exc
            self._token = str(token)
            self._expires_at = self._clock() + ttl - TOKEN_SKEW_SECONDS
            return self._token


class RateLimiter:
    """Per-carrier minimum interval plus sliding hourly budget; thread-safe."""

    def __init__(self, min_interval_seconds: float = 0.0, max_per_hour: Optional[int] = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable[[float], None]] = None) -> None:
        """
        Args:
            min_interval_seconds: Minimum gap between acquisitions.
            max_per_hour: Calls allowed in any rolling hour; None for unlimited.
            clock: Monotonic seconds source, injectable for tests.
            sleep: Sleep function, injectable for tests (defaults to time.sleep).
        """
        self.min_interval_seconds = min_interval_seconds
        self.max_per_hour = max_per_hour
        self._clock = clock
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._stamps: deque[float] = deque()
        self._last: Optional[float] = None

    def acquire(self) -> None:
        """
        Claim one call slot, sleeping to honour the minimum interval.

        Raises:
            CarrierRateLimited: The hourly budget is spent; retry_after is seconds until a slot frees.
        """
        with self._lock:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= 3600:
                self._stamps.popleft()
            if self.max_per_hour is not None and len(self._stamps) >= self.max_per_hour:
                wait = 3600 - (now - self._stamps[0])
                raise CarrierRateLimited("hourly budget spent", retry_after=max(0.0, wait))
            if self._last is not None:
                gap = self.min_interval_seconds - (now - self._last)
                if gap > 0:
                    self._sleep(gap)
                    now += gap
            self._last = now
            self._stamps.append(now)


def request_json(method: str, url: str, *, headers: Optional[dict] = None,
                 params: Optional[dict] = None, json: Optional[Any] = None,
                 timeout: float = TIMEOUT) -> Any:
    """
    Send one HTTP request expecting JSON, retrying 429 and 5xx.

    Args:
        method: HTTP verb.
        url: Absolute URL.
        headers: Request headers (may carry the bearer token; never logged).
        params: Query parameters.
        json: JSON body.
        timeout: Per-attempt timeout in seconds.

    Returns:
        The decoded JSON body.

    Raises:
        CarrierAuthError: HTTP 401/403.
        CarrierNotFound: HTTP 404.
        CarrierRateLimited: 429 persisted through every attempt.
        CarrierUnavailable: 5xx persisted, network failure, or an unreadable body.
        CarrierError: Any other 4xx.
    """
    last_status: Optional[int] = None
    last_retry_after: Optional[float] = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json,
                                    timeout=timeout)
        except requests.RequestException as exc:
            raise CarrierUnavailable(f"{method} request failed: {type(exc).__name__}") from exc

        status = resp.status_code
        if status < 400:
            try:
                return resp.json()
            except (ValueError, RecursionError) as exc:
                raise CarrierUnavailable("non-JSON response") from exc
        if status in (401, 403):
            raise CarrierAuthError(f"HTTP {status}")
        if status == 404:
            raise CarrierNotFound("HTTP 404")
        if status not in RETRY_STATUS:
            raise CarrierError(f"HTTP {status}")

        last_status = status
        last_retry_after = _retry_after(resp)
        if attempt < MAX_ATTEMPTS - 1:
            delay = _backoff(attempt)
            if last_retry_after is not None:
                delay = min(last_retry_after, 120.0)
            _sleep(delay)

    if last_status == 429:
        raise CarrierRateLimited("still rate limited after retries", retry_after=last_retry_after)
    raise CarrierUnavailable(f"still failing after {MAX_ATTEMPTS} attempts (last HTTP {last_status})")


_missing_logged: set[str] = set()


def adapters() -> dict[str, CarrierAdapter]:
    """
    Configured carrier adapters keyed by carrier code ("ups", "usps").

    Each carrier module exposes a module-level `adapter()` factory. A module that does not exist
    yet or fails to import is skipped, logged once per process.

    Returns:
        Mapping of carrier code to adapter, containing only adapters whose configured() is True.
    """
    found: dict[str, CarrierAdapter] = {}
    for code in ("ups", "usps"):
        try:
            module = importlib.import_module(f"pipeline.packages.carriers.{code}")
            candidate: CarrierAdapter = module.adapter()
        except ImportError:
            if code not in _missing_logged:
                _missing_logged.add(code)
                _log.info("carrier adapter %s unavailable (module not importable)", code)
            continue
        if candidate.configured():
            found[candidate.code] = candidate
    return found
