"""
Registrar adapter contract: the types and Protocol every registrar client (Namecheap, GoDaddy,
WordPress.com) implements, and the typed errors that stand in for their failure responses.

Design decisions:

- Registrar is a typing.Protocol, not a base class: adapters live in sibling modules
  (pipeline/domains/registrars/namecheap.py etc.) and this package must stay importable without
  pulling in any adapter's HTTP dependencies (mirrors pipeline/collectors/__init__.py's dispatch()
  doing its adapter imports locally).
- Unlike pipeline/collectors (which never raises for an expected failure, returning a
  CollectionResult instead), these methods raise typed exceptions. list_domains()/check() return
  plain lists the caller iterates directly; wrapping every element in a result/status pair would
  push the "did this fail" check onto every call site. The one caller that must distinguish failure
  kinds (api/routers/connections.py's health dispatch, per docs/plans/domain-collector/tasks.json)
  catches the specific subclass it cares about instead of string-matching a message.
- Prices are Decimal, never float: they round-trip through the DomainQuote/DomainPurchase tables
  and a purchase decision (price <= DOMAINS_MAX_PRICE) must never be a floating-point comparison.
- Exception messages must stay generic: pipeline/connections.py already forbids echoing submitted
  credentials back to the caller, and these exceptions can reach an HTTP error body, so no adapter
  may put a raw registrar response (which can include the credentials it was sent) into one.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Protocol


@dataclass(frozen=True)
class RegistrarDomain:
    """One domain as the registrar itself reports it, from list_domains()."""
    name: str  # lower-case punycode, as normalize_domain() returns
    expires_at: Optional[datetime]  # naive UTC
    auto_renew: Optional[bool]
    locked: Optional[bool]
    privacy: Optional[bool]
    nameservers: tuple[str, ...]


@dataclass(frozen=True)
class Quote:
    """An authoritative, priced availability check for one name, from check()."""
    name: str
    available: bool
    premium: bool
    price: Decimal  # first-year, account currency
    renewal_price: Optional[Decimal]
    currency: str


@dataclass(frozen=True)
class Contact:
    """
    Registrant contact for purchase(). Field names match connections._REGISTRANT_CONTACT_KEYS
    exactly, since a Contact is built directly from the registrant_contact secret's parsed JSON.
    """
    first_name: str
    last_name: str
    address1: str
    city: str
    state_province: str
    postal_code: str
    country: str
    phone: str
    email: str


@dataclass(frozen=True)
class PurchaseResult:
    """What the registrar actually did with a purchase() call."""
    name: str
    success: bool
    order_id: Optional[str]  # the registrar's own order/transaction id, when it gave one
    charged: Optional[Decimal]  # amount actually charged, account currency
    detail: str  # human-readable outcome, safe to store and to show the owner


class Registrar(Protocol):
    """
    What every registrar adapter implements. kind identifies which one for logging and UI display.
    """
    kind: str  # "namecheap" | "godaddy" | "wordpress"

    def list_domains(self) -> list[RegistrarDomain]:
        """Every domain on the account. Raises a typed error below on failure."""
        ...

    def check(self, names: Sequence[str]) -> list[Quote]:
        """
        Authoritative availability + price for each name, in the registrar's own response order.
        Raises Unsupported for a registrar with no availability API (e.g. WordPress.com).
        """
        ...

    def purchase(self, quote: Quote, years: int, contact: Contact) -> PurchaseResult:
        """
        Buys quote.name for the given term. Callers must have already enforced every purchase
        guardrail (DOMAINS_PURCHASE_ENABLED, quote freshness, price cap, daily cap): this method
        performs the charge, it does not re-check policy.
        """
        ...

    def check_connection(self) -> str:
        """A human-readable detail string on success. Raises a typed error below on failure."""
        ...


class RegistrarError(Exception):
    """Base class for every registrar-adapter failure. Never carries a raw registrar response."""


class NotEligible(RegistrarError):
    """The account doesn't meet the registrar's threshold for this operation (a minimum spend or
    domain count, e.g. GoDaddy's availability API requiring >=50 domains or $20/mo)."""


class IpNotWhitelisted(RegistrarError):
    """The registrar requires the caller's public IPv4 to be allow-listed first (Namecheap)."""


class RateLimited(RegistrarError):
    """The registrar throttled the request (HTTP 429 or an equivalent API error)."""


class AuthFailed(RegistrarError):
    """The registrar rejected the credentials on this connection."""


class Unsupported(RegistrarError):
    """This registrar kind does not implement the requested operation."""
