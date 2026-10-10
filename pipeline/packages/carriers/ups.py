"""
UPS Track API adapter (OAuth client credentials) producing base.TrackResult.

Design decisions:

- Dormant unless UPS_CLIENT_ID and UPS_CLIENT_SECRET are both set (via packages_settings());
  adapter() always returns an object, configured() tells the registry whether to use it.
- Endpoint paths, header names and every response field name are module constants, and the whole
  response shape is read in ONE function (parse_response). The shape comes from the UPS developer
  docs and was NOT observed against the live API, so a doc mismatch is a one-line constant change
  or a single-function fix.
- HTTP goes through base.request_json (retry/backoff, typed 401/403/404/429/5xx errors) and tokens
  through base.OAuthClientCredentials with HTTP Basic auth, cached until near expiry.
- Numbers that are not UPS-shaped are rejected locally with CarrierNotFound so no call is spent.
  1Z numbers must pass numbers.classify without a failed checksum; Home Depot style 1R00...
  numbers are labelled UPS ids by the merchant and are sent as-is.
- A UPS "Tracking Information Not Found" warning arrives with HTTP 200 and is mapped to
  CarrierNotFound inside parse_response.
- The RateLimiter budget (1s spacing, 250/hour) is a conservative local guess, not a published
  UPS quota; HTTP 429 from UPS is still honoured by base.request_json.
- Only extracted fields are kept; raw responses are never logged or stored.
"""
import re
import uuid
from datetime import datetime
from typing import Any, Optional

from pipeline.packages import numbers, packages_settings
from pipeline.packages.carriers import base
from pipeline.packages.carriers.base import (
    CarrierNotFound, OAuthClientCredentials, RateLimiter, TrackEvent, TrackResult,
    normalise_status,
)

TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details/{inquiry}"
TRACK_PARAMS = {"locale": "en_US", "returnSignature": "false"}
TRANSACTION_SRC = "signalslate"
MIN_INTERVAL_SECONDS = 1.0
MAX_PER_HOUR = 250

# Response field names (UPS docs).
F_ROOT, F_SHIPMENT, F_PACKAGE = "trackResponse", "shipment", "package"
F_WARNINGS, F_WARN_CODE, F_WARN_MSG = "warnings", "code", "message"
F_ACTIVITY, F_STATUS, F_TYPE, F_DESC = "activity", "status", "type", "description"
F_LOCATION, F_ADDRESS = "location", "address"
F_CITY, F_STATE, F_COUNTRY = "city", "stateProvince", "country"
F_DATE, F_TIME = "date", "time"
F_CURRENT, F_DELIVERY_DATE = "currentStatus", "deliveryDate"
NOT_FOUND_TEXT = "not found"
NOT_FOUND_CODES = frozenset({"TW0001"})

DELIVERY_DELIVERED = "DEL"
DELIVERY_ETA = ("SDD", "RDD")

# activity status.type -> package status; anything else goes through normalise_status.
TYPE_STATUS = {
    "D": "delivered", "O": "out_for_delivery", "I": "in_transit", "P": "in_transit",
    "DO": "in_transit", "DD": "in_transit", "W": "in_transit", "M": "pending",
    "X": "exception", "RS": "returned",
}

_HOME_DEPOT_RE = re.compile(r"^1R00[A-Z0-9]+$")


def _parse_when(date: Any, time_: Any = None) -> Optional[datetime]:
    """Parses UPS YYYYMMDD plus optional HHMMSS into a naive datetime; None when malformed."""
    try:
        return datetime.strptime(f"{date}{time_ or '000000'}", "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None


def _format_location(loc: Any) -> Optional[str]:
    """Formats location.address as "City, ST, US", skipping empty parts."""
    address = loc.get(F_ADDRESS) if isinstance(loc, dict) else None
    if not isinstance(address, dict):
        return None
    parts = [str(address[k]).strip() for k in (F_CITY, F_STATE, F_COUNTRY) if address.get(k)]
    return ", ".join(parts) or None


def _status_of(status: dict) -> str:
    """Maps one status object to a package status."""
    code = str(status.get(F_TYPE) or "").strip().upper()
    if code in TYPE_STATUS:
        return TYPE_STATUS[code]
    return normalise_status(status.get(F_DESC))


def parse_response(tracking_number: str, body: Any) -> TrackResult:
    """
    Converts a UPS Track details response into a TrackResult. The only place response fields are read.

    Args:
        tracking_number: The number that was queried.
        body: Decoded JSON response.

    Returns:
        TrackResult with events sorted oldest first.

    Raises:
        CarrierNotFound: Response carries a "Tracking Information Not Found" warning or no package.
    """
    shipments = (body.get(F_ROOT) or {}).get(F_SHIPMENT) if isinstance(body, dict) else None
    shipment = shipments[0] if isinstance(shipments, list) and shipments and isinstance(shipments[0], dict) else {}
    packages = shipment.get(F_PACKAGE)
    package = packages[0] if isinstance(packages, list) and packages and isinstance(packages[0], dict) else None

    for warn in shipment.get(F_WARNINGS) or []:
        if not isinstance(warn, dict):
            continue
        if (str(warn.get(F_WARN_CODE) or "").upper() in NOT_FOUND_CODES
                or NOT_FOUND_TEXT in str(warn.get(F_WARN_MSG) or "").lower()):
            raise CarrierNotFound("UPS has no tracking information for this number")
    if package is None:
        raise CarrierNotFound("UPS response has no package")

    events: list[TrackEvent] = []
    for act in package.get(F_ACTIVITY) or []:
        if not isinstance(act, dict):
            continue
        when = _parse_when(act.get(F_DATE), act.get(F_TIME))
        status = act.get(F_STATUS) if isinstance(act.get(F_STATUS), dict) else {}
        if when is None:
            continue
        events.append(TrackEvent(when, _status_of(status), str(status.get(F_DESC) or "").strip(),
                                 _format_location(act.get(F_LOCATION))))
    events.sort(key=lambda e: e.occurred_at)

    current = package.get(F_CURRENT) if isinstance(package.get(F_CURRENT), dict) else {}
    status = TYPE_STATUS.get(str(current.get(F_TYPE) or "").upper()) or (
        events[-1].status if events else normalise_status(current.get(F_DESC)))

    eta: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    for entry in package.get(F_DELIVERY_DATE) or []:
        if not isinstance(entry, dict):
            continue
        when = _parse_when(entry.get(F_DATE))
        kind = entry.get(F_TYPE)
        if when and kind == DELIVERY_DELIVERED:
            delivered_at = when
        elif when and kind in DELIVERY_ETA and eta is None:
            eta = when
    if status == "delivered" and delivered_at is None:
        delivered = [e.occurred_at for e in events if e.status == "delivered"]
        delivered_at = delivered[-1] if delivered else None
    if status == "delivered" and delivered_at is not None:
        # DEL carries a date only; prefer the scan's exact time on the same day.
        same_day = [e.occurred_at for e in events
                    if e.status == "delivered" and e.occurred_at.date() == delivered_at.date()]
        delivered_at = same_day[-1] if same_day else delivered_at
    return TrackResult(tracking_number, status, tuple(events), eta, delivered_at)


class UpsAdapter:
    """UPS Track API client; dormant without UPS_CLIENT_ID/UPS_CLIENT_SECRET."""

    code = "ups"

    def __init__(self) -> None:
        self._oauth: Optional[OAuthClientCredentials] = None
        self._oauth_key: Optional[tuple[str, str]] = None
        self._limiter = RateLimiter(MIN_INTERVAL_SECONDS, MAX_PER_HOUR)

    def configured(self) -> bool:
        """Whether both UPS credentials are set."""
        s = packages_settings()
        return bool(s.ups_client_id and s.ups_client_secret)

    def _token(self) -> str:
        s = packages_settings()
        key = (s.ups_client_id or "", s.ups_client_secret or "")
        if self._oauth is None or self._oauth_key != key:
            self._oauth = OAuthClientCredentials(TOKEN_URL, key[0], key[1], basic_auth=True)
            self._oauth_key = key
        return self._oauth.get_token()

    def track(self, tracking_number: str) -> TrackResult:
        """
        Fetches current UPS status for a tracking number.

        Args:
            tracking_number: UPS 1Z number or a 1R00... merchant-labelled UPS id.

        Returns:
            TrackResult with events oldest first.

        Raises:
            CarrierNotFound: Not a UPS-shaped number, HTTP 404, or UPS not-found warning.
            CarrierAuthError: Token or track request rejected (401/403).
            CarrierRateLimited: Local budget spent or UPS 429 after retries.
            CarrierUnavailable: Network failure or persistent 5xx.
        """
        number = re.sub(r"\s", "", tracking_number).upper()
        ups = [m for m in numbers.classify(number) if m.carrier == "ups"]
        if not (any(m.checksum is not False for m in ups) or _HOME_DEPOT_RE.match(number)):
            raise CarrierNotFound("not a UPS tracking number")
        self._limiter.acquire()
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "transId": uuid.uuid4().hex,
            "transactionSrc": TRANSACTION_SRC,
        }
        body = base.request_json("GET", TRACK_URL.format(inquiry=number), headers=headers,
                                 params=dict(TRACK_PARAMS))
        return parse_response(number, body)


def adapter() -> UpsAdapter:
    """Factory used by base.adapters()."""
    return UpsAdapter()
