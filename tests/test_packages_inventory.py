"""Tests for pipeline.packages.inventory: correlation, monotonic status, event dedupe, stale."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import Package, PackageEmail, PackageEvent  # noqa: E402
from pipeline.packages import inventory  # noqa: E402
from pipeline.packages.email_status import StatusSignal  # noqa: E402
from pipeline.packages.extract import Shipment  # noqa: E402
from pipeline.packages.inventory import MailRef  # noqa: E402

T0 = datetime(2026, 9, 1, 12, 0)
UPS = "1Z999AA10123456784"


@pytest.fixture
def session(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def ship(number=None, carrier="ups", order=None, domain="shop.example", merchant="Shop"):
    return Shipment(carrier=carrier, tracking_number=number, order_id=order, merchant=merchant,
                    merchant_domain=domain, source="test", confidence="high")


def sig(status, at=T0, eta=None):
    return StatusSignal(status=status, occurred_at=at, eta=eta, description=status)


def mail(mid="m1", thread="t1", at=T0):
    return MailRef("acct", mid, thread, "Your order " * 40, "a@shop.example", at)


def test_creation_with_signal(session):
    [p] = inventory.apply_email(session, [ship(UPS, order="A1")], sig("in_transit", eta=date(2026, 9, 5)), mail())
    assert p.status == "in_transit" and p.status_source == "email"
    assert p.eta == datetime(2026, 9, 5) and p.last_event_at == T0
    assert len(session.exec(select(PackageEvent)).all()) == 1
    assert len(session.exec(select(PackageEmail.subject)).first()) == 300


def test_numbered_without_signal_is_pending(session):
    [p] = inventory.apply_email(session, [ship(UPS)], None, mail())
    assert p.status == "pending"
    assert session.exec(select(PackageEvent)).all() == []


def test_noise_order_only_creates_nothing(session):
    assert inventory.apply_email(session, [ship(order="A1")], None, mail()) == []
    assert session.exec(select(Package)).all() == []


def test_duplicate_mail_idempotent(session):
    for _ in range(2):
        inventory.apply_email(session, [ship(UPS)], sig("in_transit"), mail())
    assert len(session.exec(select(Package)).all()) == 1
    assert len(session.exec(select(PackageEvent)).all()) == 1
    assert len(session.exec(select(PackageEmail)).all()) == 1


def test_order_level_upgrade(session):
    [a] = inventory.apply_email(session, [ship(order="A1")], sig("pending"), mail("m1"))
    assert a.tracking_number is None
    [b] = inventory.apply_email(session, [ship(UPS, order="A1")], sig("in_transit", T0 + timedelta(hours=1)), mail("m2"))
    assert b.id == a.id and b.tracking_number == UPS and b.status == "in_transit"
    assert len(session.exec(select(Package)).all()) == 1


def test_walmart_split_order(session):
    n1, n2 = "555" + "1" * 17, "555" + "2" * 17
    inventory.apply_email(session, [ship(n1, "walmart", "W9", "walmart.com")], sig("in_transit"), mail("m1"))
    inventory.apply_email(session, [ship(n2, "walmart", "W9", "walmart.com")], sig("in_transit"), mail("m2"))
    out = inventory.apply_email(session, [ship(None, "walmart", "W9", "walmart.com")],
                                sig("out_for_delivery", T0 + timedelta(hours=5)), mail("m3"))
    assert len(out) == 2 and {p.status for p in out} == {"out_for_delivery"}
    assert len(session.exec(select(Package)).all()) == 2


def test_terminal_not_reopened_and_no_demotion(session):
    [p] = inventory.apply_email(session, [ship(UPS)], sig("delivered", T0), mail("m1"))
    assert p.delivered_at == T0
    inventory.apply_email(session, [ship(UPS)], sig("in_transit", T0 + timedelta(days=1)), mail("m2"))
    inventory.apply_email(session, [ship(UPS)], sig("exception", T0 + timedelta(days=2)), mail("m3"))
    assert p.status == "delivered"
    assert len(session.exec(select(PackageEvent)).all()) == 3


def test_exception_rule(session):
    [p] = inventory.apply_email(session, [ship(UPS)], sig("out_for_delivery"), mail("m1"))
    inventory.apply_email(session, [ship(UPS)], sig("exception", T0 + timedelta(hours=1)), mail("m2"))
    assert p.status == "exception"
    inventory.apply_email(session, [ship(UPS)], sig("in_transit", T0 + timedelta(hours=2)), mail("m3"))
    assert p.status == "in_transit"
    assert not inventory.can_transition("in_transit", "pending")
    assert not inventory.can_transition("in_transit", "stale")
    assert inventory.can_transition("stale", "pending")


def test_stale_marking(session):
    old, fresh, done = (inventory.add_manual(session, n, "ups") for n in ("A", "B", "C"))
    old.created_at = fresh.created_at = done.created_at = T0
    fresh.last_event_at = T0 + timedelta(days=40)
    done.status = "delivered"
    session.flush()
    assert inventory.mark_stale(session, T0 + timedelta(days=45), 30) == 1
    assert (old.status, fresh.status, done.status) == ("stale", "pending", "delivered")
    assert inventory.mark_stale(session, T0 + timedelta(days=45), 30) == 0


def test_carrier_result(session):
    [p] = inventory.apply_email(session, [ship(UPS)], sig("in_transit"), mail())
    ev = SimpleNamespace(occurred_at=T0 + timedelta(days=1), status="delivered",
                         description="Delivered", location="NYC")
    res = SimpleNamespace(status="delivered", events=[ev], eta=None, delivered_at=ev.occurred_at)
    assert inventory.apply_carrier_result(session, p, res) is True
    assert (p.status, p.status_source, p.delivered_at) == ("delivered", "carrier", ev.occurred_at)
    assert p.last_checked_at is not None
    assert inventory.apply_carrier_result(session, p, res) is False
    assert len(session.exec(select(PackageEvent).where(PackageEvent.source == "carrier")).all()) == 1


def test_manual_add(session):
    p = inventory.add_manual(session, " 1z999aa10123456784 ", None)
    assert (p.carrier, p.tracking_number, p.status_source) == ("ups", UPS, "manual")
    assert inventory.add_manual(session, UPS, None).id == p.id
    with pytest.raises(ValueError):
        inventory.add_manual(session, "hello", None)
    assert inventory.add_manual(session, "hello", "Other").carrier == "other"


def test_no_thread_correlation(session):
    inventory.apply_email(session, [ship(order="A1")], sig("pending"), mail("m1", thread="T"))
    inventory.apply_email(session, [ship(order="B2")], sig("pending"), mail("m2", thread="T"))
    assert len(session.exec(select(Package)).all()) == 2


def test_archive_and_delete(session):
    [p] = inventory.apply_email(session, [ship(UPS)], sig("in_transit"), mail())
    assert inventory.archive(session, p.id, True).archived is True
    assert inventory.mark_stale(session, T0 + timedelta(days=99), 30) == 0
    assert inventory.delete(session, p.id) is True
    assert inventory.delete(session, p.id) is False
    assert session.exec(select(PackageEvent)).all() == []
    assert session.exec(select(PackageEmail)).all() == []
