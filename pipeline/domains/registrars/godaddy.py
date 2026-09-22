"""
GoDaddy registrar adapter: the v1 REST API at api(.ote-)godaddy.com.

Design decisions:

- Single `_auth_headers()` builds the Authorization header, and every request goes through
  `GoDaddyClient._request()`, which is the only place that calls it. certbot #10733
  (docs/_research/2026-09-21_domain-collector.md Finding 4 / Dissent) anticipated GoDaddy moving to
  Bearer PAT auth; confirmed live 2026-09-22 (a fresh developer.godaddy.com signup now issues only a
  Personal Access Token, no key/secret pair — the classic sso-key pair comes from the separate,
  deprecated classic-developer.godaddy.com). Both auth shapes work against the v1 Domains API this
  module calls, so both are supported, keyed by pipeline.connections.godaddy_auth_mode(): "pat"
  (default, a single api_token secret, `Authorization: Bearer <token>`) or "classic" (api_key +
  api_secret, `Authorization: sso-key KEY:SECRET`). Keeping both behind one function makes the header
  format a one-line-per-mode difference instead of a per-call-site hunt.
- GoDaddyClient is built directly from a connection id (pipeline.connections.get/get_secret), same
  seam as NamecheapClient: config (environment, auth_mode) comes from the connection's config,
  api_token or api_key/api_secret from its vault-backed secrets, picked by auth_mode. Building it is
  the one place this module touches pipeline.connections.
- Error mapping is by HTTP status, not response text (unlike namecheap.py): GoDaddy's error bodies
  are JSON with a `code` field, which is a stable shape to match on. 401 is a credentials problem;
  403 with `code` ACCESS_DENIED or ACCOUNT_NOT_ELIGIBLE is the availability-tier gate (Finding 4:
  needs >=50 domains or >=$20/mo average spend) and maps to NotEligible, any other 403 to AuthFailed;
  429 maps to RateLimited and includes the `RateLimit-Reset` header when present. Exception messages
  are always a fixed, generic string, never the raw response body, for the same reason as
  namecheap.py: a GoDaddy error body could echo request data.
- Availability price is documented (Research Finding 4/Q5) as being in micro-units of the account
  currency; `_PRICE_MICRO_UNITS` is a named constant with this comment so a build-time check against
  a live response is a one-line fix if wrong, never a bare `/ 1_000_000` literal. As with
  namecheap.py, check() never returns a Quote with a guessed or zero price for an *available* name:
  when the response has no price for one, that name raises RegistrarError instead.
- purchase() follows Finding 4's documented flow: GET /v1/domains/agreements for the target tld's
  agreement keys, then POST /v1/domains/purchase with consent.agreementKeys/agreedAt/agreedBy,
  privacy explicitly false, and no add-ons (no payment-method selector exists in the docs, so none
  is sent - Open Questions). agreedBy is the caller's current public IPv4, mirroring namecheap.py's
  egress-IP lookup for its own whitelist message. The four contact roles GoDaddy's schema expects
  (contactAdmin/contactBilling/contactRegistrant/contactTech) all get the same Contact, same as
  Namecheap's four-role domains.create body.
- `_LIST_LIMIT` (GET /v1/domains page size) is UNVERIFIED: the endpoint reference lists a `limit`
  and `marker` pair for pagination but not a maximum page size, so this is a conservative default,
  not a confirmed API cap. `privacy` in the v1 domains list response is UNVERIFIED (no field is
  documented; `exposeWhois` is an unconfirmed inverse-of-privacy candidate), so list_domains() reports
  it as None rather than guessing.
"""
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from pipeline import connections
from pipeline.domains.registrars._egress import current_egress_ip
from pipeline.domains.registrars import (
    AuthFailed,
    Contact,
    NotEligible,
    PurchaseResult,
    Quote,
    RateLimited,
    RegistrarDomain,
    RegistrarError,
)

_log = logging.getLogger(__name__)

_TIMEOUT = 30
_PRODUCTION_HOST = "https://api.godaddy.com"
_OTE_HOST = "https://api.ote-godaddy.com"

_DOMAINS_PATH = "/v1/domains"
_AVAILABLE_PATH = "/v1/domains/available"
_AGREEMENTS_PATH = "/v1/domains/agreements"
_PURCHASE_PATH = "/v1/domains/purchase"

_CODE_ACCESS_DENIED = "ACCESS_DENIED"
_CODE_ACCOUNT_NOT_ELIGIBLE = "ACCOUNT_NOT_ELIGIBLE"

# UNVERIFIED (module docstring): GET /v1/domains has no documented max page size; conservative
# default, not a confirmed API cap.
_LIST_LIMIT = 100

# Research Finding 4 (Implementation Sketch Step "availability"): up to 500 names per
# POST /v1/domains/available call.
_CHECK_BATCH_SIZE = 500

# UNVERIFIED (module docstring): GoDaddy availability price is documented in vendor examples as
# micro-units of the account currency. Divide by this to get a decimal amount; verify against a
# live response before trusting it for a real purchase decision.
_PRICE_MICRO_UNITS = Decimal("1000000")

# Fallback only: used when an availability entry has no currency of its own.
_ACCOUNT_CURRENCY = "USD"


@dataclass(frozen=True)
class _Config:
    auth_mode: str  # "pat" | "classic"
    api_token: Optional[str]
    api_key: Optional[str]
    api_secret: Optional[str]
    environment: str  # "production" | "ote"


def _load_config(connection_id: str) -> _Config:
    """
    Reads a godaddy connection's config and secrets. Raises AuthFailed for anything that makes the
    connection unusable, rather than a bare KeyError/ValueError, since callers treat every Registrar
    failure as one of the typed errors in registrars/__init__.py.
    """
    view = connections.get(connection_id)
    if view is None:
        raise AuthFailed("no such connection")
    mode = connections.godaddy_auth_mode(view)
    environment = str(view.config.get("environment") or "production")
    if mode == "classic":
        api_key = connections.get_secret(connection_id, "api_key")
        api_secret = connections.get_secret(connection_id, "api_secret")
        if not api_key or not api_secret:
            raise AuthFailed("connection has no GoDaddy API key/secret configured")
        return _Config(auth_mode=mode, api_token=None, api_key=api_key, api_secret=api_secret, environment=environment)
    api_token = connections.get_secret(connection_id, "api_token")
    if not api_token:
        raise AuthFailed("connection has no GoDaddy personal access token configured")
    return _Config(auth_mode=mode, api_token=api_token, api_key=None, api_secret=None, environment=environment)


def _auth_headers(config: _Config) -> dict[str, str]:
    """The one place that builds GoDaddy's Authorization header: sso-key for "classic", Bearer PAT
    for "pat" (module docstring: both accepted by the v1 Domains API this module calls)."""
    if config.auth_mode == "classic":
        return {"Authorization": f"sso-key {config.api_key}:{config.api_secret}"}
    return {"Authorization": f"Bearer {config.api_token}"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _domain_tld(name: str) -> str:
    return name.rsplit(".", 1)[-1] if "." in name else name


def _error_code(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("code") or "") if isinstance(body, dict) else ""


def _raise_for_status(response: Any) -> None:
    """
    Maps an HTTP error response to the closest typed error (module docstring: by status, not text).
    Never includes the response body in the raised message, since GoDaddy can echo request data.
    """
    status = response.status_code
    if status == 401:
        raise AuthFailed("GoDaddy rejected the API credentials")
    if status == 429:
        reset = response.headers.get("RateLimit-Reset") or response.headers.get("Retry-After")
        detail = f"GoDaddy rate limit exceeded; resets in {reset}s" if reset else "GoDaddy rate limit exceeded"
        raise RateLimited(detail)
    if status == 403:
        code = _error_code(response)
        if code in (_CODE_ACCESS_DENIED, _CODE_ACCOUNT_NOT_ELIGIBLE):
            raise NotEligible("availability needs >=50 domains or 0/mo spend")
        raise AuthFailed("GoDaddy rejected the API credentials")
    raise RegistrarError("GoDaddy API request failed")


def _parse_expires(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def _parse_domain(entry: dict[str, Any]) -> RegistrarDomain:
    name = str(entry.get("domain") or "").lower()
    auto_renew = entry.get("renewAuto")
    locked = entry.get("locked")
    return RegistrarDomain(
        name=name,
        expires_at=_parse_expires(entry.get("expires")),
        auto_renew=auto_renew if isinstance(auto_renew, bool) else None,
        locked=locked if isinstance(locked, bool) else None,
        # UNVERIFIED (module docstring): no documented privacy field on this endpoint.
        privacy=None,
        nameservers=tuple(entry.get("nameServers") or ()),
    )


def _parse_quote(entry: dict[str, Any]) -> Quote:
    name = str(entry.get("domain") or "").lower()
    available = bool(entry.get("available"))
    premium = bool(entry.get("premium"))
    currency = str(entry.get("currency") or _ACCOUNT_CURRENCY)
    if not available:
        # Never used in a purchase decision (purchase() is never called for an unavailable name),
        # so an exact price is not needed here; still never a guess for an *available* name below.
        return Quote(name=name, available=False, premium=premium, price=Decimal("0"), renewal_price=None, currency=currency)
    raw_price = entry.get("price")
    if raw_price is None:
        # Never a Quote carrying Decimal("0")/a guess for an available name: a purchase price-cap
        # guard comparing against a stand-in value would wrongly pass this name.
        raise RegistrarError(f"GoDaddy price unavailable for {name}")
    try:
        price = Decimal(str(raw_price)) / _PRICE_MICRO_UNITS
    except InvalidOperation:
        raise RegistrarError(f"GoDaddy returned an unparseable price for {name}") from None
    return Quote(name=name, available=True, premium=premium, price=price, renewal_price=None, currency=currency)


def _contact_payload(contact: Contact) -> dict[str, Any]:
    return {
        "nameFirst": contact.first_name,
        "nameLast": contact.last_name,
        "email": contact.email,
        "phone": contact.phone,
        "addressMailing": {
            "address1": contact.address1,
            "city": contact.city,
            "state": contact.state_province,
            "postalCode": contact.postal_code,
            "country": contact.country,
        },
    }


class GoDaddyClient:
    """Registrar adapter for GoDaddy's v1 REST API, built from a stored connection."""

    kind = "godaddy"

    def __init__(self, connection_id: str) -> None:
        self._connection_id = connection_id
        self._config = _load_config(connection_id)

    @property
    def _host(self) -> str:
        return _OTE_HOST if self._config.environment == "ote" else _PRODUCTION_HOST

    def _request(self, method: str, path: str, params: Optional[dict[str, str]] = None, json_body: Any = None) -> Any:
        try:
            response = requests.request(
                method,
                f"{self._host}{path}",
                headers=_auth_headers(self._config),
                params=params,
                json=json_body,
                timeout=_TIMEOUT,
            )
        except requests.RequestException:
            raise RegistrarError("GoDaddy API request failed") from None
        if response.status_code >= 400:
            _raise_for_status(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise RegistrarError("GoDaddy API returned an unparseable response") from None

    def list_domains(self) -> list[RegistrarDomain]:
        """Every domain on the account, walking GET /v1/domains's marker-based pages."""
        domains: list[RegistrarDomain] = []
        marker: Optional[str] = None
        while True:
            params = {"limit": str(_LIST_LIMIT)}
            if marker:
                params["marker"] = marker
            data = self._request("GET", _DOMAINS_PATH, params=params)
            entries = [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
            domains.extend(_parse_domain(entry) for entry in entries)
            if len(entries) < _LIST_LIMIT:
                return domains
            next_marker = entries[-1].get("domain")
            if not next_marker:
                return domains
            marker = str(next_marker)

    def check(self, names: Sequence[str]) -> list[Quote]:
        """Authoritative availability for each name via POST /v1/domains/available, chunked at 500."""
        quotes: list[Quote] = []
        names = list(names)
        for start in range(0, len(names), _CHECK_BATCH_SIZE):
            chunk = names[start:start + _CHECK_BATCH_SIZE]
            data = self._request("POST", _AVAILABLE_PATH, params={"checkType": "FULL"}, json_body=chunk)
            entries = data.get("domains") if isinstance(data, dict) else None
            entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            quotes.extend(_parse_quote(entry) for entry in entries)
        return quotes

    def _fetch_agreement_keys(self, tld: str) -> list[str]:
        """
        Agreement keys required by purchase()'s consent.agreementKeys, for one tld. Raises
        RegistrarError when the response has none, rather than sending an empty purchase consent.
        """
        data = self._request("GET", _AGREEMENTS_PATH, params={"tlds": tld, "privacy": "false"})
        items = data if isinstance(data, list) else []
        keys = [str(item["agreementKey"]) for item in items if isinstance(item, dict) and item.get("agreementKey")]
        if not keys:
            raise RegistrarError(f"GoDaddy returned no purchase agreement for .{tld}")
        return keys

    def purchase(self, quote: Quote, years: int, contact: Contact) -> PurchaseResult:
        """
        Buys quote.name via POST /v1/domains/purchase after fetching its tld's agreement keys.
        Callers must have already enforced purchase guardrails (see registrars/__init__.py's
        Registrar.purchase). privacy is explicitly false and no add-ons are sent (module docstring).
        """
        tld = _domain_tld(quote.name)
        agreement_keys = self._fetch_agreement_keys(tld)
        contact_payload = _contact_payload(contact)
        body = {
            "consent": {
                "agreedAt": _iso_now(),
                "agreedBy": current_egress_ip(),
                "agreementKeys": agreement_keys,
            },
            "contactAdmin": contact_payload,
            "contactBilling": contact_payload,
            "contactRegistrant": contact_payload,
            "contactTech": contact_payload,
            "domain": quote.name,
            "period": years,
            "privacy": False,
        }
        response = self._request("POST", _PURCHASE_PATH, json_body=body)
        order_id = None
        if isinstance(response, dict) and response.get("orderId") is not None:
            order_id = str(response["orderId"])
        return PurchaseResult(
            name=quote.name,
            success=True,
            order_id=order_id,
            # UNVERIFIED (module docstring): GoDaddy's purchase response documents no charged-amount
            # field; the quoted price is used since privacy is off and no add-ons are sent.
            charged=quote.price,
            detail="domain purchase submitted to GoDaddy",
        )

    def check_connection(self) -> str:
        """Confirms the stored credentials (pat or classic) work via GET /v1/domains?limit=1."""
        self._request("GET", _DOMAINS_PATH, params={"limit": "1"})
        return f"connected to GoDaddy ({self._config.environment})"
