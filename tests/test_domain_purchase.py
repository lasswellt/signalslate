"""
Tests for pipeline.domains.purchase: create_quote(), execute_purchase() (every refusal path, the
double-submit idempotency guard, a registrar timeout/error, and a real success) and
reconcile_unknown().

A FakeRegistrar in-process double stands in for a real adapter (registrar_factory is keyword-
injectable, mirroring pipeline.domains.inventory's own registrar_factory pattern) so these tests
never touch the network and never mock pipeline.domains.purchase or any sibling module under test.
requests.exceptions.Timeout is used directly (a true external failure mode), not mocked.
"""
import sys
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
import requests
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db  # noqa: E402
from pipeline.db import Domain, DomainPurchase, DomainQuote  # noqa: E402
from pipeline.domains import DomainSettings, purchase  # noqa: E402
from pipeline.domains.registrars import AuthFailed, Contact, PurchaseResult, Quote, RegistrarError  # noqa: E402

NOW = datetime(2026, 9, 21, 12, 0, 0)
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


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


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


@pytest.fixture
def wordpress_conn(vault):
    # wordpress has no registrant_contact secret at all (pipeline.connections.KINDS) — used to
    # exercise the missing_registrant_contact guard without faking connections.get_secret.
    connections.create("wordpress", {"label": "main", "client_id": "12345", "client_secret": "wp-secret-Qw88"})
    return "wordpress_main"


def _enabled_settings(
    *,
    purchase_enabled: bool = True,
    max_price: Decimal = Decimal("25"),
    daily_cap: Decimal = Decimal("50"),
    allow_premium: bool = False,
) -> DomainSettings:
    return DomainSettings(
        purchase_enabled=purchase_enabled, max_price=max_price, daily_cap=daily_cap, allow_premium=allow_premium,
        refresh_cron="30 5 * * *", resolver=None,
    )


class FakeRegistrar:
    """In-process fake Registrar: no network, no pipeline module mocked."""

    kind = "namecheap"

    def __init__(self, quotes: Sequence[Quote] = (), purchase_result: Optional[PurchaseResult] = None,
                 purchase_exc: Optional[Exception] = None) -> None:
        self._quotes = list(quotes)
        self._purchase_result = purchase_result
        self._purchase_exc = purchase_exc
        self.purchase_calls = 0

    def list_domains(self) -> list:
        raise NotImplementedError

    def check(self, names: Sequence[str]) -> list[Quote]:
        return list(self._quotes)

    def purchase(self, quote: Quote, years: int, contact: Contact) -> PurchaseResult:
        self.purchase_calls += 1
        if self._purchase_exc is not None:
            raise self._purchase_exc
        assert self._purchase_result is not None
        return self._purchase_result

    def check_connection(self) -> str:
        return "ok"


def _insert_quote(session, *, quote_id="11111111111111111111111111111111", name="newidea.com",
                   connection_id="namecheap_main", price="12.98", renewal_price=None, premium=False,
                   expires_at=None):
    row = DomainQuote(
        id=quote_id, name=name, connection_id=connection_id, price=price, renewal_price=renewal_price,
        currency="USD", premium=premium, expires_at=expires_at or (NOW + timedelta(minutes=5)),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    session.expunge(row)  # usable after the caller's `with` block closes the session
    return row


# --- create_quote --------------------------------------------------------------------


def test_create_quote_stores_row(namecheap_conn):
    fake = FakeRegistrar(quotes=[Quote(name="newidea.com", available=True, premium=False,
                                        price=Decimal("12.98"), renewal_price=Decimal("14.98"), currency="USD")])
    quote = purchase.create_quote("newidea.com", namecheap_conn, now=NOW, registrar_factory=lambda cid: fake)
    assert quote.price == "12.98"
    assert quote.expires_at == NOW + timedelta(minutes=5)
    with db.get_session() as session:
        assert session.get(DomainQuote, quote.id) is not None


def test_create_quote_refuses_non_positive_price(namecheap_conn):
    fake = FakeRegistrar(quotes=[Quote(name="newidea.com", available=True, premium=False,
                                        price=Decimal("0"), renewal_price=None, currency="USD")])
    with pytest.raises(RegistrarError):
        purchase.create_quote("newidea.com", namecheap_conn, now=NOW, registrar_factory=lambda cid: fake)
    with db.get_session() as session:
        assert session.exec(select(DomainQuote)).all() == []


def test_create_quote_refuses_unavailable(namecheap_conn):
    fake = FakeRegistrar(quotes=[Quote(name="newidea.com", available=False, premium=False,
                                        price=Decimal("12.98"), renewal_price=None, currency="USD")])
    with pytest.raises(RegistrarError):
        purchase.create_quote("newidea.com", namecheap_conn, now=NOW, registrar_factory=lambda cid: fake)
    with db.get_session() as session:
        assert session.exec(select(DomainQuote)).all() == []


# --- execute_purchase: refusal paths (registrar.purchase() must never be called) -----


def test_execute_purchase_disabled(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings(purchase_enabled=False))
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "purchase_disabled"
    assert fake.purchase_calls == 0


def test_execute_purchase_quote_not_found(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase("no-such-quote", "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "quote_not_found"
    assert fake.purchase_calls == 0


def test_execute_purchase_quote_expired(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn, expires_at=NOW - timedelta(seconds=1))
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "quote_expired"
    assert fake.purchase_calls == 0


def test_execute_purchase_name_mismatch(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "somethingelse.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "name_mismatch"
    assert fake.purchase_calls == 0


def test_execute_purchase_premium_blocked(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings(allow_premium=False))
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn, premium=True)
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "premium_blocked"
    assert fake.purchase_calls == 0


def test_execute_purchase_price_exceeds_cap(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings(max_price=Decimal("5")))
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn, price="12.98")
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "price_exceeds_cap"
    assert fake.purchase_calls == 0


def test_execute_purchase_daily_cap_exceeded(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings(daily_cap=Decimal("20")))
    with db.get_session() as session:
        _insert_quote(session, quote_id="q-already-1", name="already.com", connection_id=namecheap_conn)
        session.add(DomainPurchase(quote_id="q-already-1", status="succeeded", price="12.98", created_at=NOW))
        session.commit()
        quote = _insert_quote(session, quote_id="q-second", name="newidea.com", connection_id=namecheap_conn,
                               price="12.98")
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "daily_cap_exceeded"
    assert fake.purchase_calls == 0


def test_execute_purchase_daily_cap_race_exactly_one_proceeds(namecheap_conn, monkeypatch, temp_db):
    # Two DIFFERENT quotes, each under the cap alone but over it together: without the module-level
    # lock both could read "today's spend" before either commits and both would pass.
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings(daily_cap=Decimal("20")))
    with db.get_session() as session:
        quote_a = _insert_quote(session, quote_id="q-race-a", name="race-a.com", connection_id=namecheap_conn,
                                 price="15.00")
        quote_b = _insert_quote(session, quote_id="q-race-b", name="race-b.com", connection_id=namecheap_conn,
                                 price="15.00")
    fake = FakeRegistrar(purchase_result=PurchaseResult(
        name="race.com", success=True, order_id="ORD1", charged=Decimal("15.00"), detail="domain registered"
    ))
    results: dict = {}
    errors: dict = {}

    def _run(label, quote):
        try:
            results[label] = purchase.execute_purchase(
                quote.id, quote.name, now=NOW, registrar_factory=lambda cid: fake
            )
        except purchase.PurchaseRefused as exc:
            errors[label] = exc

    threads = [threading.Thread(target=_run, args=("a", quote_a)), threading.Thread(target=_run, args=("b", quote_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fake.purchase_calls == 1
    assert len(results) == 1
    assert len(errors) == 1
    assert next(iter(errors.values())).reason_code == "daily_cap_exceeded"


def test_execute_purchase_years_total_exceeds_cap(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings(max_price=Decimal("30")))
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn, price="12.98", renewal_price="14.98")
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", years=3, now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "price_exceeds_cap"
    assert fake.purchase_calls == 0


def test_execute_purchase_years_total_stored_as_price(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(
        purchase, "domain_settings", lambda: _enabled_settings(max_price=Decimal("100"), daily_cap=Decimal("100"))
    )
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn, price="12.98", renewal_price="14.98")
    fake = FakeRegistrar(purchase_result=PurchaseResult(
        name="newidea.com", success=True, order_id="ORD1", charged=Decimal("42.94"), detail="domain registered"
    ))
    result = purchase.execute_purchase(quote.id, "newidea.com", years=3, now=NOW, registrar_factory=lambda cid: fake)
    assert result.status == "succeeded"
    assert result.price == "42.94"  # 12.98 + 2 * 14.98


def test_execute_purchase_invalid_years(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", years=11, now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "invalid_years"
    assert fake.purchase_calls == 0


def test_execute_purchase_registrar_unavailable(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)

    def _broken_factory(cid):
        raise RegistrarError("boom")

    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=_broken_factory)
    assert excinfo.value.reason_code == "registrar_unavailable"
    with db.get_session() as session:
        # never reached the registrar, so no pending row was ever written for this attempt
        assert session.exec(select(DomainPurchase).where(DomainPurchase.quote_id == quote.id)).first() is None


def test_execute_purchase_missing_registrant_contact(wordpress_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=wordpress_conn)
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "missing_registrant_contact"
    assert fake.purchase_calls == 0


# --- execute_purchase: registrar call outcomes ----------------------------------------


def test_execute_purchase_success_marks_domain_owned(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar(purchase_result=PurchaseResult(
        name="newidea.com", success=True, order_id="ORD1", charged=Decimal("12.98"), detail="domain registered"
    ))
    result = purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert result.status == "succeeded"
    assert fake.purchase_calls == 1
    with db.get_session() as session:
        domain = session.exec(select(Domain).where(Domain.name == "newidea.com")).first()
        assert domain is not None
        assert domain.ownership == "owned"
        assert domain.source == "namecheap"
        assert domain.missing_since is None


def test_execute_purchase_registrar_reports_failure(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar(purchase_result=PurchaseResult(
        name="newidea.com", success=False, order_id=None, charged=None, detail="registrar declined"
    ))
    result = purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert result.status == "failed"
    assert result.detail == "registrar declined"
    with db.get_session() as session:
        assert session.exec(select(Domain).where(Domain.name == "newidea.com")).first() is None


def test_execute_purchase_registrar_error_marks_failed(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar(purchase_exc=AuthFailed("credentials rejected"))
    result = purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert result.status == "failed"
    assert result.detail is not None and "AuthFailed" in result.detail


def test_execute_purchase_timeout_marks_unknown(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar(purchase_exc=requests.exceptions.Timeout("timed out"))
    result = purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert result.status == "unknown"
    with db.get_session() as session:
        assert session.exec(select(Domain).where(Domain.name == "newidea.com")).first() is None


# --- idempotency / double submit -------------------------------------------------------


def test_execute_purchase_double_submit_calls_registrar_once(namecheap_conn, monkeypatch, temp_db):
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
    fake = FakeRegistrar(purchase_result=PurchaseResult(
        name="newidea.com", success=True, order_id="ORD1", charged=Decimal("12.98"), detail="domain registered"
    ))
    first = purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert first.status == "succeeded"

    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "already_submitted"
    assert fake.purchase_calls == 1  # never reached the registrar a second time


def test_execute_purchase_refuses_when_pending_row_already_exists(namecheap_conn, monkeypatch, temp_db):
    # Simulates a concurrent submit: a pending row for this quote_id already exists (as if another
    # request's INSERT won the race), before this call's own guards even run its INSERT.
    monkeypatch.setattr(purchase, "domain_settings", lambda: _enabled_settings())
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
        session.add(DomainPurchase(quote_id=quote.id, status="pending", price="12.98", created_at=NOW))
        session.commit()
    fake = FakeRegistrar()
    with pytest.raises(purchase.PurchaseRefused) as excinfo:
        purchase.execute_purchase(quote.id, "newidea.com", now=NOW, registrar_factory=lambda cid: fake)
    assert excinfo.value.reason_code == "already_submitted"
    assert fake.purchase_calls == 0


# --- reconcile_unknown -----------------------------------------------------------------


def test_reconcile_unknown_marks_succeeded_when_domain_owned(namecheap_conn, temp_db):
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
        session.add(DomainPurchase(quote_id=quote.id, status="unknown", price="12.98", created_at=NOW))
        session.add(Domain(name="newidea.com", ownership="owned", source="namecheap", connection_id=namecheap_conn))
        session.commit()

    changed = purchase.reconcile_unknown(now=NOW)
    assert len(changed) == 1
    assert changed[0].status == "succeeded"


def test_reconcile_unknown_flags_stale_unresolved(namecheap_conn, temp_db):
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
        session.add(DomainPurchase(
            quote_id=quote.id, status="unknown", price="12.98", created_at=NOW - timedelta(days=8)
        ))
        session.commit()

    changed = purchase.reconcile_unknown(now=NOW)
    assert len(changed) == 1
    assert changed[0].status == "unknown"
    assert changed[0].detail is not None and "flagged for manual review" in changed[0].detail


def test_reconcile_unknown_leaves_recent_unresolved_alone(namecheap_conn, temp_db):
    with db.get_session() as session:
        quote = _insert_quote(session, connection_id=namecheap_conn)
        session.add(DomainPurchase(
            quote_id=quote.id, status="unknown", price="12.98", created_at=NOW - timedelta(days=1)
        ))
        session.commit()

    changed = purchase.reconcile_unknown(now=NOW)
    assert changed == []
    with db.get_session() as session:
        row = session.exec(select(DomainPurchase).where(DomainPurchase.quote_id == quote.id)).one()
        assert row.status == "unknown"
        assert row.detail is None
