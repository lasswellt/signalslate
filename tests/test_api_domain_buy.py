"""
Tests for api.routers.domain_buy.

Router-only app with install_security (real CSRF/Origin guard), a temp DB and no network:
pipeline.domains.ideas/purchase functions and registrar_for are monkeypatched at this router
module's own import site (domain_buy_router.domain_ideas.*, domain_buy_router.domain_purchase.*,
domain_buy_router.registrar_for) rather than mocking pipeline.domains.ideas/purchase/registrars
themselves. A FakeRegistrar in-process double stands in for a real adapter, mirroring
tests/test_domain_purchase.py's own fake.
"""
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import domain_buy as domain_buy_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import connections, crypto, db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import DomainPurchase, DomainQuote  # noqa: E402
from pipeline.domains import DomainSettings  # noqa: E402
from pipeline.domains.registrars import AuthFailed, NotEligible, PurchaseResult, Quote, RegistrarError  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

CONTACT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "address1": "1 Main St",
    "city": "London",
    "state_province": "London",
    "postal_code": "AB1 2CD",
    "country": "GB",
    "phone": "+442012345678",
    "email": "ada@example.com",
}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(temp_db) -> TestClient:
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(domain_buy_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


@pytest.fixture
def vault(temp_db):
    v = crypto.Vault([crypto.generate_key()])
    connections.set_vault(v)
    yield v
    connections.set_vault(None)


@pytest.fixture
def namecheap_conn(vault):
    connections.create(
        "namecheap",
        {
            "label": "main",
            "api_user": "u",
            "username": "u",
            "client_ip": "1.2.3.4",
            "api_key": "nc-key-Ab12",
            "registrant_contact": CONTACT,
        },
    )
    return "namecheap_main"


def _enabled_settings(
    *, purchase_enabled: bool = True, max_price: Decimal = Decimal("25"), daily_cap: Decimal = Decimal("50"),
    allow_premium: bool = False,
) -> DomainSettings:
    return DomainSettings(
        purchase_enabled=purchase_enabled, max_price=max_price, daily_cap=daily_cap, allow_premium=allow_premium,
        refresh_cron="30 5 * * *", resolver=None,
    )


class FakeRegistrar:
    """In-process fake Registrar: no network, no pipeline module mocked."""

    kind = "namecheap"

    def __init__(self, quotes=(), check_exc: Optional[Exception] = None,
                 purchase_result: Optional[PurchaseResult] = None, purchase_exc: Optional[Exception] = None) -> None:
        self._quotes = list(quotes)
        self._check_exc = check_exc
        self._purchase_result = purchase_result
        self._purchase_exc = purchase_exc

    def list_domains(self):
        raise NotImplementedError

    def check(self, names):
        if self._check_exc is not None:
            raise self._check_exc
        return list(self._quotes)

    def purchase(self, quote, years, contact):
        if self._purchase_exc is not None:
            raise self._purchase_exc
        assert self._purchase_result is not None
        return self._purchase_result

    def check_connection(self) -> str:
        return "ok"


def _insert_quote(*, quote_id="11111111111111111111111111111111", name="newidea.com",
                   connection_id="namecheap_main", price="12.98", renewal_price=None, premium=False,
                   expires_at=None) -> DomainQuote:
    with db.get_session() as session:
        row = DomainQuote(
            id=quote_id, name=name, connection_id=connection_id, price=price, renewal_price=renewal_price,
            currency="USD", premium=premium, expires_at=expires_at or (utcnow() + timedelta(minutes=5)),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


# --- CSRF -------------------------------------------------------------------------------------


def test_post_without_csrf_header_is_403(client):
    resp = client.post("/api/domains/ideas", json={"seeds": ["a"], "tlds": ["com"]},
                        headers={"X-Requested-With": ""})
    assert resp.status_code == 403


# --- POST /domains/ideas -----------------------------------------------------------------------


def test_generate_ideas_combinatorial(client, monkeypatch):
    monkeypatch.setattr(domain_buy_router.domain_ideas, "generate_candidates",
                         lambda seeds, tlds: ["acme.com"])
    monkeypatch.setattr(domain_buy_router.domain_ideas, "prefilter", lambda names: {"acme.com": "likely_available"})
    resp = client.post("/api/domains/ideas", json={"seeds": ["acme"], "tlds": ["com"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == [{"name": "acme.com", "status": "likely_available"}]
    assert body["llm_reason"] is None


def test_generate_ideas_use_llm_merges_names(client, monkeypatch):
    monkeypatch.setattr(domain_buy_router.domain_ideas, "generate_candidates", lambda seeds, tlds: ["acme.com"])
    monkeypatch.setattr(domain_buy_router.domain_ideas, "llm_ideas", lambda brief: (["brainy.io"], ""))
    monkeypatch.setattr(domain_buy_router.domain_ideas, "prefilter",
                         lambda names: {n: "unknown" for n in names})
    resp = client.post("/api/domains/ideas", json={"seeds": ["acme"], "tlds": ["com"], "brief": "a brand",
                                                     "use_llm": True})
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["candidates"]]
    assert names == ["acme.com", "brainy.io"]
    assert resp.json()["llm_reason"] is None


def test_generate_ideas_use_llm_without_brief_skips_llm(client, monkeypatch):
    monkeypatch.setattr(domain_buy_router.domain_ideas, "generate_candidates", lambda seeds, tlds: ["acme.com"])
    monkeypatch.setattr(domain_buy_router.domain_ideas, "prefilter", lambda names: {"acme.com": "unknown"})
    resp = client.post("/api/domains/ideas", json={"seeds": ["acme"], "tlds": ["com"], "use_llm": True})
    assert resp.status_code == 200
    assert resp.json()["llm_reason"] == "brief is required for LLM ideas"


def test_generate_ideas_rejects_unknown_field(client):
    resp = client.post("/api/domains/ideas", json={"seeds": ["a"], "tlds": ["com"], "extra": "nope"})
    assert resp.status_code == 422


# --- POST /domains/check -----------------------------------------------------------------------


def test_check_domains_returns_quotes(client, monkeypatch):
    fake = FakeRegistrar(quotes=[Quote(name="acme.com", available=True, premium=False,
                                        price=Decimal("12.98"), renewal_price=Decimal("14.98"), currency="USD")])
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    resp = client.post("/api/domains/check", json={"names": ["acme.com"], "connection_id": "namecheap_main"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == [{"name": "acme.com", "available": True, "premium": False, "price": "12.98",
                      "renewal_price": "14.98", "currency": "USD"}]


def test_check_domains_invalid_name_is_422(client):
    resp = client.post("/api/domains/check", json={"names": ["not a domain"], "connection_id": "x"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_domain"


def test_check_domains_not_eligible_is_409(client, monkeypatch):
    fake = FakeRegistrar(check_exc=NotEligible("needs >=50 domains"))
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    resp = client.post("/api/domains/check", json={"names": ["acme.com"], "connection_id": "namecheap_main"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_eligible"


def test_check_domains_auth_failed_is_generic(client, monkeypatch):
    fake = FakeRegistrar(check_exc=AuthFailed("registrar rejected api key XYZ-secret"))
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    resp = client.post("/api/domains/check", json={"names": ["acme.com"], "connection_id": "namecheap_main"})
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["code"] == "auth_failed"
    assert "XYZ-secret" not in detail["message"]


def test_check_domains_registrar_error_is_502(client, monkeypatch):
    fake = FakeRegistrar(check_exc=RegistrarError("registrar returned no quote"))
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    resp = client.post("/api/domains/check", json={"names": ["acme.com"], "connection_id": "namecheap_main"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "registrar_error"


# --- POST /domains/quotes ----------------------------------------------------------------------


def test_create_quote_stores_row(client, monkeypatch):
    fake = FakeRegistrar(quotes=[Quote(name="acme.com", available=True, premium=False,
                                        price=Decimal("12.98"), renewal_price=None, currency="USD")])
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    resp = client.post("/api/domains/quotes", json={"name": "acme.com", "connection_id": "namecheap_main"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "acme.com"
    assert body["price"] == "12.98"
    with db.get_session() as session:
        assert session.get(DomainQuote, body["id"]) is not None


def test_create_quote_unavailable_is_502(client, monkeypatch):
    fake = FakeRegistrar(quotes=[Quote(name="acme.com", available=False, premium=False,
                                        price=Decimal("12.98"), renewal_price=None, currency="USD")])
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    resp = client.post("/api/domains/quotes", json={"name": "acme.com", "connection_id": "namecheap_main"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "registrar_error"


def test_create_quote_invalid_name_is_422(client):
    resp = client.post("/api/domains/quotes", json={"name": "not a domain", "connection_id": "namecheap_main"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_domain"


# --- POST /domains/purchases -------------------------------------------------------------------


def test_create_purchase_succeeds(client, namecheap_conn, monkeypatch):
    monkeypatch.setattr(domain_buy_router.domain_purchase, "domain_settings", lambda: _enabled_settings())
    fake = FakeRegistrar(purchase_result=PurchaseResult(name="newidea.com", success=True, order_id="ord-1",
                                                          charged=Decimal("12.98"), detail="bought"))
    monkeypatch.setattr(domain_buy_router, "registrar_for", lambda cid: fake)
    quote = _insert_quote(connection_id=namecheap_conn)
    resp = client.post("/api/domains/purchases",
                        json={"quote_id": quote.id, "confirm_name": quote.name, "years": 1})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["quote_id"] == quote.id
    assert body["name"] == "newidea.com"
    assert body["currency"] == "USD"


def test_create_purchase_refused_maps_reason_code(client, namecheap_conn, monkeypatch):
    monkeypatch.setattr(domain_buy_router.domain_purchase, "domain_settings", lambda: _enabled_settings())
    resp = client.post("/api/domains/purchases",
                        json={"quote_id": "does-not-exist", "confirm_name": "acme.com", "years": 1})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "quote_not_found"


def test_create_purchase_disabled_is_422(client, namecheap_conn, monkeypatch):
    monkeypatch.setattr(domain_buy_router.domain_purchase, "domain_settings",
                         lambda: _enabled_settings(purchase_enabled=False))
    quote = _insert_quote(connection_id=namecheap_conn)
    resp = client.post("/api/domains/purchases",
                        json={"quote_id": quote.id, "confirm_name": quote.name, "years": 1})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "purchase_disabled"


def test_create_purchase_rejects_unknown_field(client):
    resp = client.post("/api/domains/purchases", json={"quote_id": "x", "confirm_name": "y", "extra": "nope"})
    assert resp.status_code == 422


# --- GET /domains/purchases --------------------------------------------------------------------


def test_list_purchases(client, temp_db):
    with db.get_session() as session:
        session.add(DomainPurchase(quote_id="q1", status="succeeded", price="12.98", created_at=utcnow()))
        session.commit()
    resp = client.get("/api/domains/purchases")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["quote_id"] == "q1"
    assert body[0]["status"] == "succeeded"
    # No quote row for "q1": the name is absent rather than an error.
    assert body[0]["name"] is None


def test_list_purchases_names_the_domain_from_its_quote(client, namecheap_conn):
    quote = _insert_quote(connection_id=namecheap_conn, name="shiny.dev")
    with db.get_session() as session:
        session.add(DomainPurchase(quote_id=quote.id, status="succeeded", price="12.98", created_at=utcnow()))
        session.commit()
    body = client.get("/api/domains/purchases").json()
    assert [(p["name"], p["currency"]) for p in body] == [("shiny.dev", "USD")]


# --- GET /domains/purchase-settings ---------------------------------------------------------


def test_purchase_settings_no_spend(client, monkeypatch):
    monkeypatch.setattr(domain_buy_router, "domain_settings",
                         lambda: _enabled_settings(max_price=Decimal("25"), daily_cap=Decimal("50")))
    resp = client.get("/api/domains/purchase-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "enabled": True, "max_price": "25", "daily_cap": "50", "allow_premium": False, "remaining_today": "50",
    }


def test_purchase_settings_subtracts_todays_spend(client, temp_db, monkeypatch):
    monkeypatch.setattr(domain_buy_router, "domain_settings",
                         lambda: _enabled_settings(daily_cap=Decimal("50")))
    with db.get_session() as session:
        session.add(DomainPurchase(quote_id="q1", status="succeeded", price="12.98", created_at=utcnow()))
        session.add(DomainPurchase(quote_id="q2", status="failed", price="99.00", created_at=utcnow()))
        session.commit()
    resp = client.get("/api/domains/purchase-settings")
    body = resp.json()
    # "failed" never charged and is excluded; only the succeeded purchase counts against the cap.
    assert body["remaining_today"] == "37.02"


def test_purchase_settings_never_leaks_secrets(client, monkeypatch):
    monkeypatch.setattr(domain_buy_router, "domain_settings", lambda: _enabled_settings())
    resp = client.get("/api/domains/purchase-settings")
    body = resp.json()
    assert set(body.keys()) == {"enabled", "max_price", "daily_cap", "allow_premium", "remaining_today"}
