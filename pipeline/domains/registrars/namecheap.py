"""
Namecheap registrar adapter: the XML API at api(.sandbox).namecheap.com/xml.response.

Design decisions:

- XML is parsed only with defusedxml.ElementTree, never stdlib xml.etree: Namecheap's response is
  untrusted network input and stdlib ElementTree is XXE-prone (Research Finding 3). A hand-rolled
  ~200-line requests client, not a wrapper library, because the maintained wrapper forks are stale
  (same finding).
- NamecheapClient is built directly from a connection id (pipeline.connections.get/get_secret):
  config (api_user, username, client_ip, sandbox) comes from the connection's config, the api_key
  from its vault-backed secret. Building it is the one place this module touches pipeline.connections,
  so a caller iterating many registrar connections never has to know that shape.
- Error mapping is by response *text*, not by numeric Namecheap error code: the FAQ page namecheap.com/
  support/api/methods/{check,create}/ returns 403 to non-browser clients (Citation Health in
  docs/_research/2026-09-21_domain-collector.md), so no verified code table exists. This is looser
  than a code table but degrades safely — an unmatched error still becomes a generic RegistrarError.
- Exception messages are always a fixed, generic string (never the registrar's raw Errors/Error text):
  pipeline/domains/registrars/__init__.py's module docstring forbids putting a raw registrar response
  in an exception, since Namecheap can echo submitted credentials back in an error message.
- _CHECK_BATCH_SIZE and the domains.create contact parameter table are UNVERIFIED for the same
  403-to-non-browser reason (Open Questions in the research doc); each is a named constant with an
  UNVERIFIED comment instead of a bare literal, so a later confirmed value is a one-line fix.
- domains.check has no price attribute for a non-premium (standard) name, so check() makes a second
  call, namecheap.users.getPricing, to price it — never Decimal("0"): a purchase price-cap guard
  (T-017) comparing against a stand-in zero would pass every non-premium name regardless of its
  real cost. getPricing's response shape is UNVERIFIED for the same 403 reason; parsing is
  defensive and, when a price genuinely can't be found, check() raises RegistrarError for that
  domain rather than returning an untrustworthy Quote. One getPricing call per distinct TLD, cached
  for the NamecheapClient instance's lifetime, since every name in a batch sharing a TLD shares a
  price.
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from pipeline import connections
from pipeline.domains.registrars import (
    AuthFailed,
    Contact,
    IpNotWhitelisted,
    NotEligible,
    PurchaseResult,
    Quote,
    RateLimited,
    RegistrarDomain,
    RegistrarError,
)

_log = logging.getLogger(__name__)

_TIMEOUT = 30
_XML_NS_URI = "http://api.namecheap.com/xml.response"
_NS = {"nc": _XML_NS_URI}
_ENDPOINT_PATH = "/xml.response"
# Namecheap API FAQ (verified 2026-09-21, docs/_research/2026-09-21_domain-collector.md Finding 3):
# production vs. sandbox hosts, and the 50/min, 700/hour, 8000/day per-key rate limits below.
_PRODUCTION_HOST = "https://api.namecheap.com"
_SANDBOX_HOST = "https://api.sandbox.namecheap.com"
RATE_LIMIT_PER_MINUTE = 50
RATE_LIMIT_PER_HOUR = 700
RATE_LIMIT_PER_DAY = 8000

_CMD_GET_LIST = "namecheap.domains.getList"
_CMD_CHECK = "namecheap.domains.check"
_CMD_CREATE = "namecheap.domains.create"
_CMD_GET_BALANCES = "namecheap.users.getBalances"
_CMD_GET_PRICING = "namecheap.users.getPricing"

# UNVERIFIED (namecheap.com method-reference pages 403 non-browser clients, same as the other
# UNVERIFIED constants below): namecheap.users.getPricing's request/response shape. Modeled on
# publicly documented examples — ProductType="DOMAIN", ProductCategory "register"/"renew" nodes
# each holding <Product Name="<tld>"><Price Duration="1" YourPrice="..." Currency="..."/> — but not
# confirmed against a live account. _find_tld_price() parses defensively and check() treats any
# shape mismatch as "price unavailable", never as a fabricated 0.
_PRICING_CATEGORY_REGISTER = "register"
_PRICING_CATEGORY_RENEW = "renew"

# Research Finding 3 / Implementation Sketch Step 2 (verified page usage): getList is paged.
_PAGE_SIZE = 100

# UNVERIFIED (docs/_research/2026-09-21_domain-collector.md Open Questions): Namecheap's exact
# domains.check batch limit. The FAQ mentions "batch around 50" but the method-reference page
# 403s non-browser clients, so this is a conservative assumed chunk size, not a confirmed API cap.
_CHECK_BATCH_SIZE = 50

# Fallback only: used for a premium Quote (whose PremiumRegistrationPrice attribute has no
# currency of its own) and as getPricing's Currency default when that attribute is missing.
_ACCOUNT_CURRENCY = "USD"

# UNVERIFIED (same Open Question as _CHECK_BATCH_SIZE): the domains.create parameter table. This
# is the documented ICANN registrant-contact shape (RegistrantFirstName, TechFirstName, ...) for
# all four required roles, not a value confirmed against the live 403'd method page. Verify from a
# browser before this path is used for a real purchase (docs/_research/2026-09-21_domain-collector.md
# Finding 3: "domains.create requires all four contact roles").
_CONTACT_ROLES = ("Registrant", "Tech", "Admin", "AuxBilling")
_CONTACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("FirstName", "first_name"),
    ("LastName", "last_name"),
    ("Address1", "address1"),
    ("City", "city"),
    ("StateProvince", "state_province"),
    ("PostalCode", "postal_code"),
    ("Country", "country"),
    ("Phone", "phone"),
    ("EmailAddress", "email"),
)

# Reports the app's own egress IPv4 in check_connection()'s detail / IpNotWhitelisted message, so
# the UI can say "whitelist X" (Namecheap has no API to edit its own IP whitelist).
_IPIFY_URL = "https://api.ipify.org"


@dataclass(frozen=True)
class _Config:
    api_user: str
    api_key: str
    username: str
    client_ip: str
    sandbox: bool


def _load_config(connection_id: str) -> _Config:
    """
    Reads a namecheap connection's config and secret. Raises AuthFailed for anything that makes
    the connection unusable (missing, unconfigured, no key) rather than a bare KeyError/ValueError,
    since callers treat every Registrar failure as one of the typed errors in registrars/__init__.py.
    """
    view = connections.get(connection_id)
    if view is None:
        raise AuthFailed("no such connection")
    api_key = connections.get_secret(connection_id, "api_key")
    if not api_key:
        raise AuthFailed("connection has no Namecheap API key configured")
    api_user = str(view.config.get("api_user") or "")
    username = str(view.config.get("username") or "")
    client_ip = str(view.config.get("client_ip") or "")
    if not api_user or not username or not client_ip:
        raise AuthFailed("connection is missing required Namecheap configuration")
    sandbox = str(view.config.get("sandbox") or "false").strip().lower() == "true"
    return _Config(api_user=api_user, api_key=api_key, username=username, client_ip=client_ip, sandbox=sandbox)


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.strip().lower() == "true"


def _to_decimal(value: Optional[str]) -> Decimal:
    if not value:
        return Decimal("0")
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal("0")


def _raise_for_errors(root: Any) -> None:
    """
    Status="OK" returns; Status="ERROR" raises the closest typed error. Matches by message text
    (see module docstring: no verified Namecheap error-code table exists) and never includes the
    registrar's own error text in the raised message, since it can echo submitted credentials.
    """
    if root.get("Status") == "OK":
        return
    messages = [(el.text or "") for el in root.iterfind(".//nc:Errors/nc:Error", _NS)]
    lowered = " ".join(messages).lower()
    if "ip" in lowered and any(w in lowered for w in ("whitelist", "not registered", "not found", "access denied")):
        raise IpNotWhitelisted("Namecheap rejected the request: caller IP is not whitelisted")
    if "eligib" in lowered or ("api access" in lowered and ("not enabled" in lowered or "disabled" in lowered)):
        raise NotEligible("Namecheap API is not enabled for this account")
    if "too many requests" in lowered or "rate limit" in lowered:
        raise RateLimited("Namecheap rate limit exceeded")
    if "apikey" in lowered.replace(" ", "") or "api key" in lowered or "authentication" in lowered or "invalid username" in lowered:
        raise AuthFailed("Namecheap rejected the API credentials")
    raise RegistrarError("Namecheap API request failed")


def _current_egress_ip() -> str:
    """Best-effort current public IPv4, for the "whitelist X" message. Never raises."""
    try:
        response = requests.get(_IPIFY_URL, params={"format": "text"}, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.text.strip()
    except requests.RequestException:
        return "unknown"


def _parse_domain(entry: Any) -> RegistrarDomain:
    name = (entry.get("Name") or "").lower()
    expires_raw = entry.get("Expires")
    expires_at: Optional[datetime] = None
    if expires_raw:
        try:
            expires_at = datetime.strptime(expires_raw, "%m/%d/%Y")
        except ValueError:
            expires_at = None
    whois_guard = entry.get("WhoisGuard")
    privacy = whois_guard.strip().upper() == "ENABLED" if whois_guard else None
    return RegistrarDomain(
        name=name,
        expires_at=expires_at,
        auto_renew=_parse_bool(entry.get("AutoRenew")),
        locked=_parse_bool(entry.get("IsLocked")),
        privacy=privacy,
        # domains.getList has no nameservers field; dns.getHosts per domain is out of scope here.
        nameservers=(),
    )


def _domain_tld(name: str) -> str:
    return name.rsplit(".", 1)[-1] if "." in name else name


def _find_tld_price(category: Any, tld: str) -> Optional[tuple[Decimal, str]]:
    """
    Within one <ProductCategory> node, the 1-year price for <Product Name="tld">, or None when the
    category, the product or a usable price attribute is missing. Prefers YourPrice (the account's
    actual price after any account-specific discount) over Price (list price); see the UNVERIFIED
    note on _CMD_GET_PRICING for why this is parsed defensively rather than assumed present.
    """
    if category is None:
        return None
    for product in category.iterfind("nc:Product", _NS):
        if (product.get("Name") or "").strip().lower() != tld:
            continue
        for price_el in product.iterfind("nc:Price", _NS):
            if price_el.get("Duration") != "1":
                continue
            raw = price_el.get("YourPrice") or price_el.get("Price")
            if not raw:
                continue
            try:
                return Decimal(raw), (price_el.get("Currency") or _ACCOUNT_CURRENCY)
            except InvalidOperation:
                continue
    return None


def _contact_params(contact: Contact) -> dict[str, str]:
    params: dict[str, str] = {}
    for role in _CONTACT_ROLES:
        for suffix, field_name in _CONTACT_FIELDS:
            params[f"{role}{suffix}"] = getattr(contact, field_name)
    return params


class NamecheapClient:
    """Registrar adapter for Namecheap's XML API, built from a stored connection."""

    kind = "namecheap"

    def __init__(self, connection_id: str) -> None:
        self._connection_id = connection_id
        self._config = _load_config(connection_id)
        # tld -> (register_price, renewal_price, currency), or None when getPricing had nothing
        # for that tld. Instance-lifetime only, per the module docstring's pricing design decision.
        self._pricing_cache: dict[str, Optional[tuple[Decimal, Optional[Decimal], str]]] = {}

    @property
    def _host(self) -> str:
        return _SANDBOX_HOST if self._config.sandbox else _PRODUCTION_HOST

    def _call(self, command: str, **params: str) -> Any:
        query = {
            "ApiUser": self._config.api_user,
            "ApiKey": self._config.api_key,
            "UserName": self._config.username,
            "ClientIp": self._config.client_ip,
            "Command": command,
            **params,
        }
        try:
            response = requests.get(f"{self._host}{_ENDPOINT_PATH}", params=query, timeout=_TIMEOUT)
        except requests.RequestException:
            raise RegistrarError("Namecheap API request failed") from None
        try:
            root = ET.fromstring(response.content)
        except (ValueError, DefusedXmlException):
            # ValueError covers ET.ParseError (malformed XML); DefusedXmlException covers a
            # rejected XXE/entity-expansion attempt (defusedxml.common.EntitiesForbidden etc).
            raise RegistrarError("Namecheap API returned an unparseable or unsafe response") from None
        _raise_for_errors(root)
        return root

    def list_domains(self) -> list[RegistrarDomain]:
        """Every domain on the account, walking namecheap.domains.getList's PageSize=100 pages."""
        domains: list[RegistrarDomain] = []
        page = 1
        while True:
            root = self._call(_CMD_GET_LIST, ListType="ALL", PageSize=str(_PAGE_SIZE), Page=str(page))
            result = root.find(".//nc:CommandResponse/nc:DomainGetListResult", _NS)
            entries = list(result.iterfind("nc:Domain", _NS)) if result is not None else []
            domains.extend(_parse_domain(entry) for entry in entries)
            if len(entries) < _PAGE_SIZE:
                return domains
            page += 1

    def _fetch_register_pricing(self, tld: str) -> Optional[tuple[Decimal, Optional[Decimal], str]]:
        """
        1-year register + renew price for tld from namecheap.users.getPricing, cached per tld for
        this client's lifetime (every name in a check() batch sharing a tld shares a price, and
        purchase() prices are looked up right before a spend decision, so staleness risk is low).

        Returns None when the response has no usable register price for this tld — the caller must
        not fall back to 0. A registrar-level failure (auth, rate limit, ...) is not caught here and
        propagates as-is from self._call, since that is a real typed error, not "price unavailable".
        """
        if tld in self._pricing_cache:
            return self._pricing_cache[tld]
        root = self._call(_CMD_GET_PRICING, ProductType="DOMAIN", ProductName=tld)
        pricing_result = root.find(".//nc:CommandResponse/nc:UserGetPricingResult", _NS)
        result: Optional[tuple[Decimal, Optional[Decimal], str]] = None
        if pricing_result is not None:
            register_price: Optional[Decimal] = None
            renewal_price: Optional[Decimal] = None
            currency = _ACCOUNT_CURRENCY
            for product_type in pricing_result.iterfind("nc:ProductType", _NS):
                if (product_type.get("Name") or "").strip().upper() != "DOMAIN":
                    continue
                for category in product_type.iterfind("nc:ProductCategory", _NS):
                    category_name = (category.get("Name") or "").strip().lower()
                    found = _find_tld_price(category, tld)
                    if found is None:
                        continue
                    price, found_currency = found
                    if category_name == _PRICING_CATEGORY_REGISTER:
                        register_price, currency = price, found_currency
                    elif category_name == _PRICING_CATEGORY_RENEW:
                        renewal_price = price
            if register_price is not None:
                result = (register_price, renewal_price, currency)
        self._pricing_cache[tld] = result
        return result

    def _parse_quote(self, entry: Any) -> Quote:
        name = (entry.get("Domain") or "").lower()
        premium = _parse_bool(entry.get("IsPremiumName")) or False
        if premium:
            price = _to_decimal(entry.get("PremiumRegistrationPrice"))
            renewal_price: Optional[Decimal] = _to_decimal(entry.get("PremiumRenewalPrice"))
            currency = _ACCOUNT_CURRENCY
        else:
            tld = _domain_tld(name)
            pricing = self._fetch_register_pricing(tld)
            if pricing is None:
                # Never a Quote carrying Decimal("0"): a purchase price-cap guard comparing
                # against a stand-in zero would wrongly pass every name priced this way.
                raise RegistrarError(f"Namecheap price unavailable for .{tld}")
            price, renewal_price, currency = pricing
        return Quote(
            name=name,
            available=_parse_bool(entry.get("Available")) or False,
            premium=premium,
            price=price,
            renewal_price=renewal_price,
            currency=currency,
        )

    def check(self, names: Sequence[str]) -> list[Quote]:
        """Authoritative availability for each name, chunked at _CHECK_BATCH_SIZE per call."""
        quotes: list[Quote] = []
        names = list(names)
        for start in range(0, len(names), _CHECK_BATCH_SIZE):
            chunk = names[start:start + _CHECK_BATCH_SIZE]
            root = self._call(_CMD_CHECK, DomainList=",".join(chunk))
            response = root.find(".//nc:CommandResponse", _NS)
            entries = list(response.iterfind("nc:DomainCheckResult", _NS)) if response is not None else []
            quotes.extend(self._parse_quote(entry) for entry in entries)
        return quotes

    def purchase(self, quote: Quote, years: int, contact: Contact) -> PurchaseResult:
        """
        Registers quote.name via namecheap.domains.create, charging the account balance. Callers
        must have already enforced purchase guardrails (see registrars/__init__.py's Registrar.purchase).
        """
        params = {"DomainName": quote.name, "Years": str(years)}
        params.update(_contact_params(contact))
        root = self._call(_CMD_CREATE, **params)
        result = root.find(".//nc:CommandResponse/nc:DomainCreateResult", _NS)
        if result is None:
            raise RegistrarError("Namecheap did not return a create result")
        registered = _parse_bool(result.get("Registered")) or False
        charged_raw = result.get("ChargedAmount")
        return PurchaseResult(
            name=quote.name,
            success=registered,
            order_id=result.get("OrderID"),
            charged=_to_decimal(charged_raw) if charged_raw else None,
            detail="domain registered" if registered else "registrar reported the purchase did not complete",
        )

    def check_connection(self) -> str:
        """
        Calls namecheap.users.getBalances to confirm the credentials and whitelisted IP work, and
        always reports the app's current egress IPv4 so the UI can say "whitelist X" — on success
        (confirms the right IP is whitelisted) and, via the IpNotWhitelisted message below, on failure.
        """
        egress_ip = _current_egress_ip()
        try:
            root = self._call(_CMD_GET_BALANCES)
        except IpNotWhitelisted:
            raise IpNotWhitelisted(
                f"Namecheap rejected the request: caller IP is not whitelisted; whitelist {egress_ip} in Namecheap"
            ) from None
        result = root.find(".//nc:CommandResponse/nc:UserGetBalancesResult", _NS)
        currency = result.get("Currency", "") if result is not None else ""
        balance = result.get("AvailableBalance", "") if result is not None else ""
        return f"connected as {self._config.api_user}; balance {balance} {currency}; egress IP {egress_ip} (whitelist this in Namecheap if it changes)"
