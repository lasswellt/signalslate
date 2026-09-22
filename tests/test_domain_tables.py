"""
Tests for the Domain / DomainSnapshot / DomainQuote / DomainPurchase tables in pipeline.db.

Coverage mirrors tests/test_connection_tables.py: create_all() on a temp engine, then exercise the
constraints that matter for correctness rather than every column. The two constraints that matter
here are Domain.name uniqueness (one row per real-world domain) and DomainPurchase.quote_id
uniqueness (the whole double-submit guard for a spending route).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import Connection, Domain, DomainPurchase, DomainQuote, DomainSnapshot  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _connection(session, connection_id="namecheap_main"):
    conn = Connection(id=connection_id, kind="namecheap", label="main", origin="ui", config="{}")
    session.add(conn)
    session.commit()
    return conn


def test_create_all_makes_domain_tables(temp_db):
    assert {"domain", "domainsnapshot", "domainquote", "domainpurchase"} <= set(SQLModel.metadata.tables)


def test_domain_round_trip_and_snapshot(temp_db):
    with Session(temp_db) as session:
        _connection(session)
        domain = Domain(name="example.com", ownership="owned", source="namecheap", connection_id="namecheap_main")
        session.add(domain)
        session.commit()
        session.refresh(domain)
        assert domain.id is not None

        snap = DomainSnapshot(domain_id=domain.id, data='{"ns": ["ns1.example.com"]}', data_hash="abc123")
        session.add(snap)
        session.commit()

    with Session(temp_db) as session:
        stored = session.exec(select(Domain).where(Domain.name == "example.com")).one()
        assert (stored.ownership, stored.source, stored.connection_id) == ("owned", "namecheap", "namecheap_main")
        assert stored.expires_at is None and stored.missing_since is None
        assert isinstance(stored.first_seen, datetime) and isinstance(stored.last_seen, datetime)

        snaps = session.exec(select(DomainSnapshot).where(DomainSnapshot.domain_id == stored.id)).all()
        assert len(snaps) == 1
        assert snaps[0].data_hash == "abc123"


def test_domain_name_is_unique(temp_db):
    with Session(temp_db) as session:
        session.add(Domain(name="dupe.com", source="manual"))
        session.commit()

    with Session(temp_db) as session:
        session.add(Domain(name="dupe.com", source="manual"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_domain_quote_and_purchase_round_trip(temp_db):
    with Session(temp_db) as session:
        _connection(session)
        quote = DomainQuote(
            id="11111111-1111-1111-1111-111111111111",
            name="newidea.com",
            connection_id="namecheap_main",
            price="12.98",
            renewal_price="14.98",
            currency="USD",
            premium=False,
            expires_at=utcnow() + timedelta(minutes=5),
        )
        session.add(quote)
        session.commit()

        purchase = DomainPurchase(quote_id=quote.id, status="pending", price="12.98")
        session.add(purchase)
        session.commit()

    with Session(temp_db) as session:
        stored_quote = session.get(DomainQuote, "11111111-1111-1111-1111-111111111111")
        assert stored_quote is not None and stored_quote.premium is False
        stored_purchase = session.exec(select(DomainPurchase).where(DomainPurchase.quote_id == stored_quote.id)).one()
        assert (stored_purchase.status, stored_purchase.price) == ("pending", "12.98")


def test_domain_purchase_quote_id_is_unique(temp_db):
    with Session(temp_db) as session:
        _connection(session)
        quote = DomainQuote(
            id="22222222-2222-2222-2222-222222222222",
            name="another.com",
            connection_id="namecheap_main",
            price="9.99",
            currency="USD",
            expires_at=utcnow() + timedelta(minutes=5),
        )
        session.add(quote)
        session.add(DomainPurchase(quote_id=quote.id, status="pending", price="9.99"))
        session.commit()

    with Session(temp_db) as session:
        session.add(DomainPurchase(quote_id="22222222-2222-2222-2222-222222222222", status="pending", price="9.99"))
        with pytest.raises(IntegrityError):
            session.commit()
