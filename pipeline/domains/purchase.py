"""
Domain purchasing: server-issued quotes and the guarded, idempotent purchase flow (Research Q6,
docs/plans/domain-collector/plan.md Implementation Step 4). This module spends the owner's real
money — correctness beats brevity here.

Design decisions:

- create_quote() is the only place a DomainQuote row is written, and it refuses to store one for a
  non-available name or a non-positive price (registrar bug or a taken domain the caller checked
  anyway) rather than persisting a quote a purchase could never legitimately act on. DomainQuote
  (pipeline/db.py, T-002 — fixed schema, not touched by this task) has no `available` column, so a
  stored row's mere existence IS the "was available at quote time" fact: execute_purchase() does not
  re-check availability as a separate guard because there is nothing left to check — a quote that
  failed availability never made it into the table. When execute_purchase later hands the row to
  registrar.purchase() it rebuilds a registrars.Quote with available=True for exactly this reason.
- execute_purchase() evaluates every guard — purchase enabled, quote exists/unexpired, confirm_name
  matches, premium policy, price bounds, the daily spend cap, registrant_contact present — before
  any network call, and raises a typed PurchaseRefused(reason_code) the instant one fails. Guards
  run inside one DB session/transaction so the daily-cap sum and the quote row are read from a
  single consistent snapshot.
- Idempotency is a UNIQUE DomainPurchase.quote_id, not an application-level lock: the pending row is
  inserted and committed BEFORE registrar.purchase() is called. A second call for the same quote
  (concurrent or a client retry) fails the INSERT with IntegrityError and is refused
  ("already_submitted") — registrar.purchase() is never reachable twice for one quote. This is also
  why the DB session used for guard evaluation is closed before the registrar call: a purchase is a
  slow external HTTP request and must not hold a transaction (or the SQLite single-writer lock)
  across it.
- Outcome mapping after registrar.purchase() is called: a typed RegistrarError -> "failed" (detail
  is the exception class name + its message, which RegistrarError's own contract already keeps free
  of secrets/raw registrar responses — see pipeline/domains/registrars/__init__.py). A
  requests timeout/connection error, or any other unexpected exception, means we genuinely do not
  know whether the registrar charged the account, so it becomes "unknown" — never "failed" (that
  would let a caller safely retry a purchase that may have already gone through) and never silently
  retried (this module never calls registrar.purchase() a second time for the same quote on its
  own). reconcile_unknown() is the only way an "unknown" purchase is ever resolved, and it is a
  separate, explicit, operator/scheduler-triggered step.
- reconcile_unknown() resolves an "unknown" purchase to "succeeded" once inventory.sync_all() (run
  by the caller first) shows the quoted name as an owned Domain row. An unknown purchase older than
  7 days whose domain still doesn't appear is left "unknown" (never guessed at as "failed" — the
  registrar side of an ambiguous timeout stays genuinely unknowable from here) but gets a "flagged
  for manual review" detail so the owner can chase it up with the registrar directly.
- Every time-dependent decision (quote TTL, "today" for the daily cap, purchase age for reconcile)
  takes an injectable `now` (naive UTC, pipeline.clock.utcnow() default — mirrors
  inventory.refresh_snapshots' own `now` parameter) so tests control expiry/today deterministically
  without patching the system clock.
- `years` multiplies the charge: registrar.purchase(quote, years, ...) charges quote.price for year
  one and (years - 1) * (renewal_price, or price when the registrar didn't quote one) for the rest.
  The max_price and daily_cap guards run against that full `total`, not the first-year price alone,
  or a large `years` would buy past both caps using only the first year's number. `total` (not
  quote.price) is what gets stored as DomainPurchase.price and counted by future daily-cap checks.
- Guard evaluation, the daily-cap sum, building the registrar adapter and the pending-row INSERT all
  happen under one module-level threading.Lock (_purchase_lock), not just one DB transaction: two
  concurrent calls for *different* quotes can each read "today's spend" before either commits, so a
  transaction alone does not stop both from passing the cap together. This assumes a single process
  (one uvicorn worker) — a multi-process deployment would need a DB-level advisory lock instead. The
  lock is released before registrar.purchase() itself, which still runs unlocked (a slow external
  call must not serialize every other purchase attempt behind it, nor hold a DB transaction open).
- The registrar adapter is built (registrar_factory(connection_id)) inside the same locked section,
  *before* the pending row is inserted: if that fails (bad/deleted connection, missing secrets) we
  refuse ("registrar_unavailable") with no pending row written at all, rather than recording a
  purchase attempt for a registrar call that was never actually reachable.
"""
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from uuid import uuid4

import requests
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from pipeline import connections, db
from pipeline.clock import utcnow
from pipeline.db import Domain, DomainPurchase, DomainQuote
from pipeline.domains import domain_settings, normalize_domain
from pipeline.domains.inventory import registrar_for
from pipeline.domains.registrars import Contact, Quote, Registrar, RegistrarError

_log = logging.getLogger(__name__)

_QUOTE_TTL = timedelta(minutes=5)
# Statuses that represent money already spent or still possibly-about-to-be-spent today, for the
# daily cap check. "failed" is excluded: a failed purchase never charged, so it must not eat cap.
_DAILY_CAP_STATUSES = ("succeeded", "pending", "unknown")
_RECONCILE_FLAG_AGE = timedelta(days=7)
_MIN_YEARS = 1
_MAX_YEARS = 10
# Guards + registrar-adapter construction + the pending-row INSERT all run under this lock; see the
# module docstring for why a DB transaction alone isn't enough to stop a same-day-cap race.
_purchase_lock = threading.Lock()


class PurchaseRefused(Exception):
    """execute_purchase() refused to proceed; reason_code identifies which guard failed. Raised
    before any network call — registrar.purchase() is never reached on this path."""

    def __init__(self, reason_code: str, detail: Optional[str] = None) -> None:
        self.reason_code = reason_code
        self.detail = detail or reason_code
        super().__init__(self.detail)


def create_quote(
    name: str,
    connection_id: str,
    *,
    now: Optional[datetime] = None,
    registrar_factory: Callable[[str], Registrar] = registrar_for,
) -> DomainQuote:
    """
    Issues and stores a short-lived (5 min) price quote for one name via the connection's
    registrar.check().

    Args:
        name: Domain as typed by the caller; any form normalize_domain() accepts.
        connection_id: A registrar connection id (pipeline.domains.inventory.registrar_for).
        now: The instant the quote is issued from. Defaults to pipeline.clock.utcnow().
        registrar_factory: Builds a Registrar from a connection id. Defaults to registrar_for;
            tests inject a fake instead of hitting a real adapter/network.

    Returns:
        The stored DomainQuote row (detached from its session; every field already loaded).

    Raises:
        ValueError: name fails normalize_domain().
        UnknownRegistrar / AuthFailed: connection_id has no registrar adapter, or is missing
            required config/secrets (raised by registrar_factory).
        RegistrarError: the registrar's check() failed, returned no quote for the name, reported
            the name unavailable, or returned a non-positive price — none of these is ever stored.
    """
    normalized, _tld = normalize_domain(name)
    registrar = registrar_factory(connection_id)
    quotes = registrar.check([normalized])
    quote = next((item for item in quotes if item.name == normalized), None)
    if quote is None:
        raise RegistrarError(f"registrar returned no quote for {normalized!r}")
    if not quote.available:
        raise RegistrarError(f"{normalized!r} is not available; refusing to store a quote")
    if quote.price <= 0:
        raise RegistrarError(f"registrar returned a non-positive price for {normalized!r}; refusing to store a quote")

    clock_now = now if now is not None else utcnow()
    row = DomainQuote(
        id=uuid4().hex,
        name=quote.name,
        connection_id=connection_id,
        price=str(quote.price),
        renewal_price=str(quote.renewal_price) if quote.renewal_price is not None else None,
        currency=quote.currency,
        premium=quote.premium,
        expires_at=clock_now + _QUOTE_TTL,
    )
    with db.get_session() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


def _load_contact(connection_id: str) -> Contact:
    """
    Loads and parses the connection's registrant_contact secret into a Contact.

    Raises:
        PurchaseRefused("missing_registrant_contact"): no secret stored, or it fails to parse as
            the expected JSON object (pipeline.connections' own write-side validation already
            enforces the shape on the way in; this is defense in depth, not the primary check).
    """
    raw = connections.get_secret(connection_id, "registrant_contact")
    if not raw:
        raise PurchaseRefused("missing_registrant_contact", "connection has no registrant contact on file")
    try:
        parsed = json.loads(raw)
        return Contact(**{f.name: parsed[f.name] for f in dataclass_fields(Contact)})
    except (ValueError, KeyError, TypeError) as exc:
        raise PurchaseRefused("missing_registrant_contact", "stored registrant contact is malformed") from exc


def _mark_domain_owned(session: "db.Session", name: str, connection_id: str, source_kind: str, now: datetime) -> None:
    """
    Upserts Domain after a successful purchase. Only sets what a PurchaseResult actually tells us
    (name, connection, registrar kind); expires_at/auto_renew/locked/privacy are left for the next
    inventory.sync_all() run to fill in from the registrar's own list_domains().
    """
    row = session.exec(select(Domain).where(Domain.name == name)).first()
    if row is None:
        row = Domain(name=name, ownership="owned", source=source_kind, connection_id=connection_id, first_seen=now)
    row.ownership = "owned"
    row.source = source_kind
    row.connection_id = connection_id
    row.last_seen = now
    row.missing_since = None
    session.add(row)


def execute_purchase(
    quote_id: str,
    confirm_name: str,
    years: int = 1,
    *,
    now: Optional[datetime] = None,
    registrar_factory: Callable[[str], Registrar] = registrar_for,
) -> DomainPurchase:
    """
    Buys a previously quoted domain, guarded and idempotent.

    Every guard below is checked, in order, before any network call; the first one that fails
    raises PurchaseRefused and registrar.purchase() is never reached:

    1. purchase enabled (domain_settings().purchase_enabled)
    2. the quote exists and has not expired
    3. confirm_name, normalized, equals the quote's name (explicit re-confirmation of what is
       being bought, since this call spends money)
    4. the quote is not premium, unless DOMAINS_ALLOW_PREMIUM (domain_settings().allow_premium)
    5. years is between 1 and 10
    6. the first-year price is positive; the full `total` charge (first year + (years - 1) *
       renewal, see module docstring) does not exceed domain_settings().max_price
    7. today's (UTC) succeeded + pending + unknown purchase total, plus this `total`, does not
       exceed domain_settings().daily_cap
    8. the connection has a registrant_contact secret that parses
    9. the registrar adapter for the connection can actually be built

    (The quote being for an available name is enforced at create_quote() time — see this module's
    docstring — so there is no separate "available" guard here.)

    Guards 1-9 (including building the registrar adapter) run under a module-level lock together
    with the pending-row INSERT — see the module docstring for why a DB transaction alone can't stop
    two different quotes from racing past the daily cap together. Once every guard passes, a
    DomainPurchase(status="pending") row is inserted with quote_id as its unique key and committed
    immediately — this is the whole idempotency guarantee: a concurrent or retried call for the same
    quote_id fails that INSERT and is refused ("already_submitted") rather than ever reaching the
    registrar twice. The lock is released and the registrar is called only after this commit, with
    no DB transaction held open across it. This module never retries a registrar call automatically.

    Args:
        quote_id: A DomainQuote.id previously returned by create_quote().
        confirm_name: The domain name as re-typed/re-confirmed by the caller.
        years: Registration term in years (1-10), passed through to registrar.purchase().
        now: The instant guards are evaluated against ("today" for the daily cap, quote expiry).
            Defaults to pipeline.clock.utcnow().
        registrar_factory: Builds a Registrar from a connection id. Defaults to registrar_for;
            tests inject a fake instead of hitting a real adapter/network.

    Returns:
        The final DomainPurchase row: status is "succeeded", "failed" or "unknown" (never left
        "pending" — every path below updates it before returning).

    Raises:
        PurchaseRefused: any guard above failed. registrar.purchase() was never called.
    """
    clock_now = now if now is not None else utcnow()
    settings = domain_settings()
    if not settings.purchase_enabled:
        raise PurchaseRefused("purchase_disabled", "domain purchasing is disabled")

    try:
        confirmed_name, _tld = normalize_domain(confirm_name)
    except ValueError:
        confirmed_name = None  # falls through to the name_mismatch guard below

    with _purchase_lock, db.get_session() as session:
        quote = session.get(DomainQuote, quote_id)
        if quote is None:
            raise PurchaseRefused("quote_not_found", f"no such quote {quote_id!r}")
        if quote.expires_at <= clock_now:
            raise PurchaseRefused("quote_expired", "quote has expired; request a new one")
        if confirmed_name != quote.name:
            raise PurchaseRefused("name_mismatch", "confirm_name does not match the quoted domain")
        if quote.premium and not settings.allow_premium:
            raise PurchaseRefused("premium_blocked", "premium domains are disabled")
        if not (_MIN_YEARS <= years <= _MAX_YEARS):
            raise PurchaseRefused("invalid_years", f"years must be between {_MIN_YEARS} and {_MAX_YEARS}")

        price = Decimal(quote.price)
        if price <= 0:
            raise PurchaseRefused("invalid_price", "quote has a non-positive price")
        renewal_price = Decimal(quote.renewal_price) if quote.renewal_price is not None else None
        total = price + (years - 1) * (renewal_price if renewal_price is not None else price)
        if total > settings.max_price:
            raise PurchaseRefused("price_exceeds_cap", f"total {total} for {years} year(s) exceeds max price {settings.max_price}")

        today_start = datetime(clock_now.year, clock_now.month, clock_now.day)
        today_end = today_start + timedelta(days=1)
        todays_purchases = session.exec(
            select(DomainPurchase)
            .where(col(DomainPurchase.status).in_(_DAILY_CAP_STATUSES))
            .where(col(DomainPurchase.created_at) >= today_start)
            .where(col(DomainPurchase.created_at) < today_end)
        ).all()
        spent_today = sum((Decimal(row.price) for row in todays_purchases), Decimal("0"))
        if spent_today + total > settings.daily_cap:
            raise PurchaseRefused(
                "daily_cap_exceeded", f"{spent_today + total} would exceed daily cap {settings.daily_cap}"
            )

        contact = _load_contact(quote.connection_id)

        try:
            registrar = registrar_factory(quote.connection_id)
        except Exception as exc:
            raise PurchaseRefused(
                "registrar_unavailable", f"could not build registrar adapter: {type(exc).__name__}"
            ) from exc

        purchase = DomainPurchase(quote_id=quote.id, status="pending", price=str(total), created_at=clock_now)
        session.add(purchase)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise PurchaseRefused("already_submitted", "a purchase for this quote was already submitted") from None
        session.refresh(purchase)
        purchase_id = purchase.id
        quote_name = quote.name
        quote_connection_id = quote.connection_id
        quote_premium = quote.premium
        quote_currency = quote.currency
        quote_renewal_price = renewal_price
    # Lock released and session closed here, deliberately: the registrar call below is a slow
    # external request and must not hold the lock or a DB transaction open across it.

    try:
        registrar_quote = Quote(
            name=quote_name,
            available=True,  # guaranteed by create_quote(); see this module's docstring
            premium=quote_premium,
            price=price,
            renewal_price=quote_renewal_price,
            currency=quote_currency,
        )
        result = registrar.purchase(registrar_quote, years, contact)
    except RegistrarError as exc:
        status, detail = "failed", f"{type(exc).__name__}: {exc}"
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        status, detail = "unknown", "registrar call timed out or the connection failed; purchase outcome unknown"
    except Exception as exc:  # noqa: BLE001 — genuinely unknown outcome, never guessed at as failed
        _log.warning("execute_purchase: unexpected error calling registrar: %s", type(exc).__name__)
        status, detail = "unknown", f"unexpected error calling registrar: {type(exc).__name__}"
    else:
        status = "succeeded" if result.success else "failed"
        detail = result.detail

    with db.get_session() as session:
        purchase = session.get(DomainPurchase, purchase_id)
        assert purchase is not None  # just inserted above in this same process
        purchase.status = status
        purchase.detail = detail
        session.add(purchase)
        if status == "succeeded":
            assert registrar is not None
            _mark_domain_owned(session, quote_name, quote_connection_id, registrar.kind, clock_now)
        session.commit()
        session.refresh(purchase)
        session.expunge(purchase)
        return purchase


def reconcile_unknown(*, now: Optional[datetime] = None) -> list[DomainPurchase]:
    """
    Re-examines every DomainPurchase in status "unknown" against the current Domain table. Callers
    should run inventory.sync_all() first so Domain reflects the registrar's current state.

    A quoted name that now appears as a live (missing_since is None) owned Domain row means the
    registrar purchase actually went through despite the earlier ambiguous outcome -> "succeeded".
    An unknown purchase older than 7 days whose domain still doesn't appear is left "unknown" (its
    registrar-side outcome stays genuinely unknowable from here — never guessed at as "failed") but
    its detail is updated to flag it for manual review, once.

    Args:
        now: The instant purchase age is measured against. Defaults to pipeline.clock.utcnow().

    Returns:
        Every DomainPurchase row this call actually changed (resolved to succeeded, or newly
        flagged), detached from its session.
    """
    clock_now = now if now is not None else utcnow()
    changed: list[int] = []
    with db.get_session() as session:
        unknowns = session.exec(select(DomainPurchase).where(DomainPurchase.status == "unknown")).all()
        for purchase in unknowns:
            quote = session.get(DomainQuote, purchase.quote_id)
            if quote is None:
                continue  # quote row itself is gone; nothing to reconcile against
            domain_row = session.exec(select(Domain).where(Domain.name == quote.name)).first()
            assert purchase.id is not None  # purchase came from a select() on the DB
            if domain_row is not None and domain_row.missing_since is None and domain_row.ownership == "owned":
                purchase.status = "succeeded"
                purchase.detail = "reconciled: domain now appears as owned in the portfolio"
                session.add(purchase)
                changed.append(purchase.id)
                continue
            already_flagged = bool(purchase.detail) and "flagged for manual review" in purchase.detail
            if not already_flagged and (clock_now - purchase.created_at) > _RECONCILE_FLAG_AGE:
                prefix = f"{purchase.detail}; " if purchase.detail else ""
                purchase.detail = f"{prefix}flagged for manual review: unresolved after 7 days"
                session.add(purchase)
                changed.append(purchase.id)
        session.commit()
        results = []
        for purchase_id in changed:
            row = session.get(DomainPurchase, purchase_id)
            assert row is not None
            session.refresh(row)
            session.expunge(row)
            results.append(row)
        return results
