"""
Tests for the Package / PackageEvent / PackageEmail tables in pipeline.db.

Covers the constraints that matter: the two partial-unique Package identities (tracking number,
and order-level), PackageEvent.dedupe_hash, PackageEmail's composite key, and the privacy rule
that none of the three tables has a body-like column.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import Package, PackageEmail, PackageEvent  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def test_create_all_makes_package_tables(temp_db):
    assert {"package", "packageevent", "packageemail"} <= set(SQLModel.metadata.tables)


def test_no_body_like_columns():
    for name in ("package", "packageevent", "packageemail"):
        cols = set(SQLModel.metadata.tables[name].columns.keys())
        assert not cols & {"body", "html", "text"}, name


def test_carrier_tracking_unique(temp_db):
    with Session(db.engine) as s:
        s.add(Package(carrier="ups", tracking_number="1Z999"))
        s.commit()
        s.add(Package(carrier="ups", tracking_number="1Z999"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(Package(carrier="fedex", tracking_number="1Z999"))  # different carrier ok
        s.commit()


def test_order_level_unique(temp_db):
    with Session(db.engine) as s:
        s.add(Package(carrier="amazon", merchant_domain="amazon.com", order_id="111-1"))
        s.commit()
        s.add(Package(carrier="amazon", merchant_domain="amazon.com", order_id="111-1"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(Package(carrier="amazon", merchant_domain="amazon.com", order_id="111-2"))
        s.add(Package(carrier="amazon", merchant_domain="amazon.com", order_id="111-1", tracking_number="TBA1"))
        s.commit()


def test_event_dedupe_hash_unique(temp_db):
    with Session(db.engine) as s:
        p = Package(carrier="ups", tracking_number="1Z1")
        s.add(p)
        s.commit()
        s.add(PackageEvent(package_id=p.id, occurred_at=utcnow(), status="in_transit", dedupe_hash="h1"))
        s.commit()
        s.add(PackageEvent(package_id=p.id, occurred_at=utcnow(), status="in_transit", dedupe_hash="h1"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_package_email_unique(temp_db):
    with Session(db.engine) as s:
        p1 = Package(carrier="ups", tracking_number="1Z1")
        p2 = Package(carrier="ups", tracking_number="1Z2")
        s.add(p1)
        s.add(p2)
        s.commit()
        s.add(PackageEmail(package_id=p1.id, account_label="main", message_id="m1"))
        s.commit()
        s.add(PackageEmail(package_id=p2.id, account_label="main", message_id="m1"))  # same msg, other pkg ok
        s.commit()
        s.add(PackageEmail(package_id=p1.id, account_label="main", message_id="m1"))
        with pytest.raises(IntegrityError):
            s.commit()
