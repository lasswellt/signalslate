"""
Domain buying routes: ideas, an authoritative registrar check, quotes and purchases (Research
Finding 8/Q5/Q6, docs/plans/domain-collector/plan.md/spec.md). Companion to api/routers/domains.py
(portfolio inventory); this file is the spend-money side.

Design decisions:

- Every pipeline.domains.{ideas,purchase} function this router calls is reached through the
  module (domain_ideas.generate_candidates, domain_purchase.create_quote, ...) so tests can
  monkeypatch at this file's own import site, mirroring api/routers/domains.py. registrar_for is
  imported by name and looked up again at call time (not bound as a default argument at import
  time), specifically so tests can monkeypatch `domain_buy.registrar_for` and have that same fake
  reach pipeline.domains.purchase.create_quote()/execute_purchase() via their own
  registrar_factory= keyword — those functions bind their `registrar_factory` default at def time,
  so only an explicit registrar_factory=registrar_for argument at call time stays patchable here.
- POST /domains/check calls registrar.check() directly (not create_quote()): it is an unpriced,
  no-side-effect "is this available" probe a caller can run over a whole shortlist before spending
  a quote on any one of them.
- RegistrarError (and every typed subclass in pipeline.domains.registrars, plus
  pipeline.domains.inventory.UnknownRegistrar) is mapped to a coded HTTP response by _registrar_error():
  subclasses first, then a 502 fallback for the bare base class. Every adapter's own contract
  already keeps its exception messages free of raw registrar responses/credentials (see
  pipeline/domains/registrars/__init__.py's module docstring), so str(exc) is safe to return here —
  except AuthFailed, which this route flattens to one fixed generic message regardless of adapter
  detail, since "the registrar rejected these credentials" is all a caller needs to act on.
- POST /domains/purchases only needs to catch PurchaseRefused: execute_purchase() already catches
  every RegistrarError (and the registrar-adapter-construction failure) internally and turns each
  into either a PurchaseRefused("registrar_unavailable") guard failure or a "failed"/"unknown"
  DomainPurchase row — a RegistrarError can never escape execute_purchase() itself (see its own
  docstring). PurchaseRefused.reason_code is returned as-is as the 422 `code`; every reason_code
  pipeline.domains.purchase.execute_purchase() raises is already a fixed, credential-free string.
- GET /domains/purchase-settings exposes only enabled/max_price/daily_cap/allow_premium and a
  derived remaining_today (daily_cap minus today's succeeded+pending+unknown spend, floored at 0)
  — never a secret, a connection id or a registrant contact. _DAILY_CAP_STATUSES here duplicates
  pipeline.domains.purchase's own (private, so not imported) tuple of the same name; keep the two
  in sync if that guard's accounting ever changes.
- Every handler here is plain `def`: ideas' prefilter() and llm_ideas(), and every registrar/quote/
  purchase call, are blocking network or DB I/O and run in FastAPI's threadpool (connections.py/
  domains.py convention).
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from api.serialize import iso_z
from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import DomainPurchase
from pipeline.domains import domain_settings, normalize_domain
from pipeline.domains import ideas as domain_ideas
from pipeline.domains import purchase as domain_purchase
from pipeline.domains.inventory import UnknownRegistrar, registrar_for
from pipeline.domains.registrars import (
    AuthFailed,
    IpNotWhitelisted,
    NotEligible,
    RateLimited,
    RegistrarError,
    Unsupported,
)

router = APIRouter(tags=["domain_buy"])

_log = logging.getLogger(__name__)

_MAX_SEEDS = 20
_MAX_TLDS = 20
_MAX_CHECK_NAMES = 50
_MAX_BRIEF_CHARS = 1000
# Mirrors pipeline.domains.purchase._DAILY_CAP_STATUSES; see this module's docstring.
_DAILY_CAP_STATUSES = ("succeeded", "pending", "unknown")


class IdeasBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seeds: list[str] = Field(min_length=1, max_length=_MAX_SEEDS)
    tlds: list[str] = Field(min_length=1, max_length=_MAX_TLDS)
    brief: Optional[str] = Field(default=None, max_length=_MAX_BRIEF_CHARS)
    use_llm: bool = False


class CandidateOut(BaseModel):
    name: str
    status: str


class IdeasOut(BaseModel):
    candidates: list[CandidateOut]
    llm_reason: Optional[str] = None


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(min_length=1, max_length=_MAX_CHECK_NAMES)
    connection_id: str


class QuoteOut(BaseModel):
    name: str
    available: bool
    premium: bool
    price: str
    renewal_price: Optional[str]
    currency: str


class CreateQuoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    connection_id: str


class StoredQuoteOut(BaseModel):
    id: str
    name: str
    connection_id: str
    price: str
    renewal_price: Optional[str]
    currency: str
    premium: bool
    expires_at: Optional[str]


class PurchaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str
    confirm_name: str
    years: int = 1


class PurchaseOut(BaseModel):
    id: int
    quote_id: str
    status: str
    price: str
    created_at: Optional[str]
    detail: Optional[str]


class PurchaseSettingsOut(BaseModel):
    enabled: bool
    max_price: str
    daily_cap: str
    allow_premium: bool
    remaining_today: str


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _registrar_error(exc: RegistrarError) -> HTTPException:
    """Maps a typed RegistrarError (or UnknownRegistrar) to a coded HTTP response. Subclasses are
    checked before the RegistrarError base case; see this module's docstring for why str(exc) is
    safe for every branch except AuthFailed."""
    if isinstance(exc, NotEligible):
        return _coded(409, "not_eligible", str(exc))
    if isinstance(exc, RateLimited):
        return _coded(429, "rate_limited", str(exc))
    if isinstance(exc, AuthFailed):
        return _coded(401, "auth_failed", "the registrar rejected this connection's credentials")
    if isinstance(exc, IpNotWhitelisted):
        return _coded(409, "ip_not_whitelisted", str(exc))
    if isinstance(exc, Unsupported):
        return _coded(400, "unsupported", str(exc))
    if isinstance(exc, UnknownRegistrar):
        return _coded(404, "connection_not_found", str(exc))
    return _coded(502, "registrar_error", str(exc))


def _quote_out(row) -> StoredQuoteOut:
    return StoredQuoteOut(
        id=row.id,
        name=row.name,
        connection_id=row.connection_id,
        price=row.price,
        renewal_price=row.renewal_price,
        currency=row.currency,
        premium=row.premium,
        expires_at=iso_z(row.expires_at),
    )


def _purchase_out(row: DomainPurchase) -> PurchaseOut:
    assert row.id is not None  # row came from a select() or a just-committed insert, always has a primary key
    return PurchaseOut(
        id=row.id,
        quote_id=row.quote_id,
        status=row.status,
        price=row.price,
        created_at=iso_z(row.created_at),
        detail=row.detail,
    )


@router.post("/domains/ideas", response_model=IdeasOut)
def generate_ideas(body: IdeasBody) -> IdeasOut:
    """Combinatorial seeds x tlds candidates, optionally extended with LLM brainstorming, then
    scored with the private local prefilter (never a registrar/third-party search call)."""
    names = domain_ideas.generate_candidates(body.seeds, body.tlds)
    llm_reason: Optional[str] = None
    if body.use_llm:
        if body.brief is None or not body.brief.strip():
            llm_reason = "brief is required for LLM ideas"
        else:
            llm_names, reason = domain_ideas.llm_ideas(body.brief)
            llm_reason = reason or None
            for name in llm_names:
                if name not in names:
                    names.append(name)
    statuses = domain_ideas.prefilter(names)
    candidates = [CandidateOut(name=name, status=statuses.get(name, "unknown")) for name in names]
    return IdeasOut(candidates=candidates, llm_reason=llm_reason)


@router.post("/domains/check", response_model=list[QuoteOut])
def check_domains(body: CheckBody) -> list[QuoteOut]:
    """Authoritative, priced availability check via the connection's registrar. NotEligible (an
    account below the registrar's threshold for this API) surfaces as 409 not_eligible."""
    normalized_names: list[str] = []
    for raw in body.names:
        try:
            name, _tld = normalize_domain(raw)
        except ValueError as exc:
            raise _coded(422, "invalid_domain", str(exc)) from None
        normalized_names.append(name)
    try:
        registrar = registrar_for(body.connection_id)
        quotes = registrar.check(normalized_names)
    except RegistrarError as exc:
        raise _registrar_error(exc) from None
    return [
        QuoteOut(
            name=q.name,
            available=q.available,
            premium=q.premium,
            price=str(q.price),
            renewal_price=str(q.renewal_price) if q.renewal_price is not None else None,
            currency=q.currency,
        )
        for q in quotes
    ]


@router.post("/domains/quotes", status_code=201, response_model=StoredQuoteOut)
def create_quote(body: CreateQuoteBody) -> StoredQuoteOut:
    """Issues and stores a short-lived (5 min) price quote for one name."""
    try:
        row = domain_purchase.create_quote(body.name, body.connection_id, registrar_factory=registrar_for)
    except ValueError as exc:
        raise _coded(422, "invalid_domain", str(exc)) from None
    except RegistrarError as exc:
        raise _registrar_error(exc) from None
    return _quote_out(row)


@router.post("/domains/purchases", status_code=201, response_model=PurchaseOut)
def create_purchase(body: PurchaseBody) -> PurchaseOut:
    """Buys a previously quoted domain. Every PurchaseRefused guard failure (quote missing/expired,
    name mismatch, premium/price/daily-cap policy, missing registrant contact, an already-submitted
    quote_id, ...) surfaces as 422 with that guard's own reason_code."""
    try:
        row = domain_purchase.execute_purchase(
            body.quote_id, body.confirm_name, body.years, registrar_factory=registrar_for
        )
    except domain_purchase.PurchaseRefused as exc:
        raise _coded(422, exc.reason_code, exc.detail) from None
    return _purchase_out(row)


@router.get("/domains/purchases", response_model=list[PurchaseOut])
def list_purchases() -> list[PurchaseOut]:
    """Every purchase attempt, most recent first."""
    with db.get_session() as session:
        rows = session.exec(select(DomainPurchase).order_by(col(DomainPurchase.created_at).desc())).all()
    return [_purchase_out(row) for row in rows]


@router.get("/domains/purchase-settings", response_model=PurchaseSettingsOut)
def get_purchase_settings() -> PurchaseSettingsOut:
    """Purchase policy as currently configured, plus today's remaining spend headroom. Never a
    secret, connection id or registrant contact."""
    settings = domain_settings()
    now = utcnow()
    today_start = datetime(now.year, now.month, now.day)
    today_end = today_start + timedelta(days=1)
    with db.get_session() as session:
        todays_purchases = session.exec(
            select(DomainPurchase)
            .where(col(DomainPurchase.status).in_(_DAILY_CAP_STATUSES))
            .where(col(DomainPurchase.created_at) >= today_start)
            .where(col(DomainPurchase.created_at) < today_end)
        ).all()
    spent_today = sum((Decimal(row.price) for row in todays_purchases), Decimal("0"))
    remaining = settings.daily_cap - spent_today
    if remaining < 0:
        remaining = Decimal("0")
    return PurchaseSettingsOut(
        enabled=settings.purchase_enabled,
        max_price=str(settings.max_price),
        daily_cap=str(settings.daily_cap),
        allow_premium=settings.allow_premium,
        remaining_today=str(remaining),
    )
