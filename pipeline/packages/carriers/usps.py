"""
USPS Tracking v3 adapter (OAuth client credentials) producing base.TrackResult.

Design decisions:

- Dormant unless USPS_CLIENT_ID and USPS_CLIENT_SECRET are both set (via packages_settings());
  adapter() always returns an object, configured() tells the registry whether to use it.
- Endpoint paths and every response field name are module constants, and the whole response shape
  is read in ONE function (parse_response). The shape comes from the USPS developer docs and was
  NOT observed against the live API, so a doc mismatch is a one-line constant change or a
  single-function fix.
- HTTP goes through base.request_json (retry/backoff, typed 401/403/404/429/5xx errors) and tokens
  through base.OAuthClientCredentials in form auth style (client_id/client_secret in the form
  body). USPS documents a JSON body for the token call too; the form style is used because the
  shared OAuth helper supports it and OAuth2 servers are required to accept it.
- IMpb numbers carrying a routing prefix (420 + 5 or 9 digit ZIP) are reduced to the trailing 22
  digits before lookup. Numbers that are not USPS-shaped (numbers.classify must report a usps
  match whose checksum has not failed) are rejected locally with CarrierNotFound so no call or
  budget slot is spent.
- DEFAULT_HOURLY_BUDGET (60/hour) is a third-party figure (research Q3), not a published USPS
  quota; HTTP 429 from USPS is still honoured by base.request_json.
- Timestamps carry UTC offsets and are converted to naive UTC (project convention).
- Only extracted fields are kept; raw responses are never logged or stored.
"""
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from pipeline.packages import numbers, packages_settings
from pipeline.packages.carriers import base
from pipeline.packages.carriers.base import (
    CarrierNotFound, OAuthClientCredentials, RateLimiter, TrackEvent, TrackResult,
    normalise_status,
)

TOKEN_URL = "https://apis.usps.com/oauth2/v3/token"
TRACK_URL = "https://apis.usps.com/tracking/v3/tracking/{number}"
TRACK_PARAMS = {"expand": "DETAIL"}
MIN_INTERVAL_SECONDS = 1.0
DEFAULT_HOURLY_BUDGET = 60  # third-party figure (research Q3), not a published USPS quota

# Response field names (USPS docs).
F_NUMBER, F_CATEGORY, F_STATUS, F_EXPECTED = "trackingNumber", "statusCategory", "status", "expectedDeliveryDate"
F_EVENTS, F_TYPE, F_TIMESTAMP = "trackingEvents", "eventType", "eventTimestamp"
F_CITY, F_STATE, F_COUNTRY = "eventCity", "eventState", "eventCountry"
F_ERROR = "error"

# statusCategory (lower-cased) -> package status; anything else goes through normalise_status.
CATEGORY_STATUS = {
    "pre-shipment": "pending", "accepted": "in_transit", "in transit": "in_transit",
    "available for pickup": "in_transit", "out for delivery": "out_for_delivery",
    "delivered": "delivered", "alert": "exception", "return to sender": "returned",
}

_ROUTING_PREFIX_RE = re.compile(r"^420\d{5}(?:\d{4})?(\d{22})$")


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parses an ISO 8601 timestamp to naive UTC; offset-less values are taken as UTC."""
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_date(value: Any) -> Optional[datetime]:
    """Parses YYYY-MM-DD into a naive midnight datetime; None when malformed."""
    try:
        d = date.fromisoformat(str(value).strip()[:10])
    except (ValueError, TypeError):
        return None
    return datetime(d.year, d.month, d.day)


def _format_location(event: dict) -> Optional[str]:
    """Formats city/state/country as "City, ST, US", skipping empty parts."""
    parts = [str(event[k]).strip() for k in (F_CITY, F_STATE, F_COUNTRY) if event.get(k)]
    return ", ".join(parts) or None


def _category_status(category: Any) -> Optional[str]:
    """Maps a statusCategory to a package status; None when absent or unrecognised."""
    key = str(category or "").strip().lower()
    if not key:
        return None
    mapped = CATEGORY_STATUS.get(key) or normalise_status(key)
    return None if mapped == "unknown" else mapped


def parse_response(tracking_number: str, body: Any) -> TrackResult:
    """
    Converts a USPS Tracking v3 response into a TrackResult. The only place response fields are read.

    Args:
        tracking_number: The number that was queried.
        body: Decoded JSON response.

    Returns:
        TrackResult with events sorted oldest first.

    Raises:
        CarrierNotFound: Response is an error body or carries no tracking data.
    """
    if not isinstance(body, dict) or body.get(F_ERROR) or not (
            body.get(F_CATEGORY) or body.get(F_EVENTS) or body.get(F_NUMBER)):
        raise CarrierNotFound("USPS has no tracking information for this number")

    events: list[TrackEvent] = []
    for ev in body.get(F_EVENTS) or []:
        if not isinstance(ev, dict):
            continue
        when = _parse_timestamp(ev.get(F_TIMESTAMP))
        if when is None:
            continue
        text = str(ev.get(F_TYPE) or "").strip()
        events.append(TrackEvent(when, normalise_status(text), text, _format_location(ev)))
    events.sort(key=lambda e: e.occurred_at)

    status = (_category_status(body.get(F_CATEGORY))
              or (events[-1].status if events else normalise_status(body.get(F_STATUS))))
    eta = _parse_date(body.get(F_EXPECTED)) if status != "delivered" else None
    delivered_at = None
    if status == "delivered":
        delivered = [e.occurred_at for e in events if e.status == "delivered"]
        delivered_at = delivered[-1] if delivered else (events[-1].occurred_at if events else None)
    return TrackResult(tracking_number, status, tuple(events), eta, delivered_at)


def normalise_number(tracking_number: str) -> str:
    """
    Cleans a USPS tracking number for lookup.

    Args:
        tracking_number: Raw number, possibly with spaces and an IMpb 420+ZIP routing prefix.

    Returns:
        The number without whitespace; the trailing 22 digits when a 420+ZIP prefix is present.

    Raises:
        CarrierNotFound: The number is not USPS-shaped or fails its checksum.
    """
    number = re.sub(r"\s", "", tracking_number).upper()
    prefixed = _ROUTING_PREFIX_RE.match(number)
    if prefixed:
        number = prefixed.group(1)
    if not any(m.carrier == "usps" and m.checksum is not False for m in numbers.classify(number)):
        raise CarrierNotFound("not a USPS tracking number")
    return number


class UspsAdapter:
    """USPS Tracking v3 client; dormant without USPS_CLIENT_ID/USPS_CLIENT_SECRET."""

    code = "usps"

    def __init__(self) -> None:
        self._oauth: Optional[OAuthClientCredentials] = None
        self._oauth_key: Optional[tuple[str, str]] = None
        self._limiter = RateLimiter(MIN_INTERVAL_SECONDS, DEFAULT_HOURLY_BUDGET)

    def configured(self) -> bool:
        """Whether both USPS credentials are set."""
        s = packages_settings()
        return bool(s.usps_client_id and s.usps_client_secret)

    def _token(self) -> str:
        s = packages_settings()
        key = (s.usps_client_id or "", s.usps_client_secret or "")
        if self._oauth is None or self._oauth_key != key:
            self._oauth = OAuthClientCredentials(TOKEN_URL, key[0], key[1], basic_auth=False)
            self._oauth_key = key
        return self._oauth.get_token()

    def track(self, tracking_number: str) -> TrackResult:
        """
        Fetches current USPS status for a tracking number.

        Args:
            tracking_number: USPS number, optionally with an IMpb 420+ZIP routing prefix.

        Returns:
            TrackResult with events oldest first.

        Raises:
            CarrierNotFound: Not a USPS-shaped number, HTTP 404, or an error body.
            CarrierAuthError: Token or track request rejected (401/403).
            CarrierRateLimited: Local budget spent or USPS 429 after retries.
            CarrierUnavailable: Network failure or persistent 5xx.
        """
        number = normalise_number(tracking_number)
        self._limiter.acquire()
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        body = base.request_json("GET", TRACK_URL.format(number=number), headers=headers,
                                 params=dict(TRACK_PARAMS))
        return parse_response(number, body)


def adapter() -> UspsAdapter:
    """Factory used by base.adapters()."""
    return UspsAdapter()
