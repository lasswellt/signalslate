"""Tests for pipeline.packages.poll.refresh_due using a temp sqlite engine and real fake adapters."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import Package, PackageEvent  # noqa: E402
from pipeline.packages import poll  # noqa: E402
from pipeline.packages.carriers import base  # noqa: E402
from pipeline.packages.carriers.base import TrackEvent, TrackResult  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, 0)


class FakeAdapter:
    def __init__(self, code, results=None, error=None, errors_by_number=None):
        self.code = code
        self.results = results or {}
        self.error = error
        self.errors_by_number = errors_by_number or {}
        self.calls = []

    def configured(self):
        return True

    def track(self, tracking_number):
        self.calls.append(tracking_number)
        if tracking_number in self.errors_by_number:
            raise self.errors_by_number[tracking_number]
        if self.error:
            raise self.error
        return self.results.get(tracking_number) or TrackResult(tracking_number, "in_transit")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _add(engine, **kw):
    kw.setdefault("carrier", "ups")
    kw.setdefault("tracking_number", f"1Z{len(str(kw))}{id(kw)}")
    with Session(engine) as s:
        p = Package(**kw)
        s.add(p)
        s.commit()
        return p.id


def _get(engine, pid):
    with Session(engine) as s:
        return s.get(Package, pid)


def test_no_adapters_polls_nothing(temp_db):
    _add(temp_db, tracking_number="A1")
    res = poll.refresh_due(NOW, adapters_override={})
    assert (res.polled, res.changed, res.errors) == (0, 0, {})


def test_exclusions(temp_db):
    ok = _add(temp_db, tracking_number="OK")
    _add(temp_db, tracking_number="ARCH", archived=True)
    _add(temp_db, tracking_number="RET", status="returned")
    _add(temp_db, tracking_number="LATER", next_check_at=NOW + timedelta(hours=1))
    _add(temp_db, carrier="walmart", tracking_number="W1")
    _add(temp_db, carrier="amazon", tracking_number="AZ1")
    _add(temp_db, carrier="unknown", tracking_number="U1")
    _add(temp_db, carrier="ups", tracking_number=None, merchant_domain="x.com", order_id="9")
    ups = FakeAdapter("ups")
    res = poll.refresh_due(NOW, adapters_override={"ups": ups})
    assert ups.calls == ["OK"]
    assert res.polled == 1
    assert _get(temp_db, ok).next_check_at == NOW + timedelta(hours=2)


def test_oldest_first_and_cadence(temp_db):
    _add(temp_db, tracking_number="NEW", next_check_at=NOW - timedelta(hours=1))
    _add(temp_db, tracking_number="OLD", next_check_at=NOW - timedelta(hours=5))
    ups = FakeAdapter("ups")
    poll.refresh_due(NOW, adapters_override={"ups": ups})
    assert ups.calls == ["OLD", "NEW"]


def test_delivered_window(temp_db):
    _add(temp_db, tracking_number="RECENT", status="delivered", delivered_at=NOW - timedelta(hours=47))
    _add(temp_db, tracking_number="OLDD", status="delivered", delivered_at=NOW - timedelta(hours=49))
    ups = FakeAdapter("ups")
    poll.refresh_due(NOW, adapters_override={"ups": ups})
    assert ups.calls == ["RECENT"]


def test_status_change_applied_with_events(temp_db):
    pid = _add(temp_db, tracking_number="T1", status="pending")
    res_t = TrackResult("T1", "delivered", events=(TrackEvent(NOW - timedelta(hours=1), "delivered", "Delivered"),),
                        delivered_at=NOW - timedelta(hours=1))
    res = poll.refresh_due(NOW, adapters_override={"ups": FakeAdapter("ups", results={"T1": res_t})})
    assert (res.polled, res.changed) == (1, 1)
    pkg = _get(temp_db, pid)
    assert pkg.status == "delivered" and pkg.last_error is None and pkg.last_checked_at == NOW
    with Session(temp_db) as s:
        assert s.exec(select(PackageEvent).where(PackageEvent.package_id == pid)).all()


def test_not_found_backoff(temp_db):
    pid = _add(temp_db, tracking_number="NF")
    poll.refresh_due(NOW, adapters_override={"ups": FakeAdapter("ups", error=base.CarrierNotFound("x"))})
    pkg = _get(temp_db, pid)
    assert pkg.last_error == "not found"
    assert pkg.next_check_at == NOW + timedelta(hours=12)


def test_unavailable_records_class_name(temp_db):
    pid = _add(temp_db, tracking_number="UN")
    res = poll.refresh_due(NOW, adapters_override={"ups": FakeAdapter("ups", error=base.CarrierUnavailable("x"))})
    pkg = _get(temp_db, pid)
    assert pkg.last_error == "CarrierUnavailable"
    assert pkg.next_check_at == NOW + timedelta(hours=2)
    assert res.errors == {"ups": "CarrierUnavailable"}


def test_rate_limit_stops_only_that_carrier(temp_db):
    u1 = _add(temp_db, tracking_number="U1", next_check_at=NOW - timedelta(hours=3))
    _add(temp_db, tracking_number="U2", next_check_at=NOW - timedelta(hours=2))
    s1 = _add(temp_db, carrier="usps", tracking_number="S1")
    ups = FakeAdapter("ups", error=base.CarrierRateLimited())
    usps = FakeAdapter("usps")
    res = poll.refresh_due(NOW, adapters_override={"ups": ups, "usps": usps})
    assert ups.calls == ["U1"]
    assert usps.calls == ["S1"]
    assert res.errors == {"ups": "rate limited"}
    assert _get(temp_db, u1).next_check_at == NOW - timedelta(hours=3)
    assert _get(temp_db, s1).next_check_at == NOW + timedelta(hours=2)


def test_auth_error_recorded_other_carrier_continues(temp_db):
    u1 = _add(temp_db, tracking_number="U1")
    _add(temp_db, carrier="usps", tracking_number="S1")
    ups = FakeAdapter("ups", error=base.CarrierAuthError("bad"))
    usps = FakeAdapter("usps")
    res = poll.refresh_due(NOW, adapters_override={"ups": ups, "usps": usps})
    assert res.errors == {"ups": "not authorized"}
    assert res.polled == 1
    pkg = _get(temp_db, u1)
    assert pkg.next_check_at is None and pkg.last_error is None
