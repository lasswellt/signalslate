"""
Tests for pipeline.collectors.domains.collect_domains(): no network (this collector never makes
one — see its module docstring), so these exercise the DB-read + diff() wiring directly against a
temp SQLite engine, following tests/test_domain_inventory.py's temp_db fixture.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.collectors import CollectionResult, Item  # noqa: E402
from pipeline.collectors.domains import collect_domains  # noqa: E402
from pipeline.db import Domain, DomainSnapshot  # noqa: E402

SINCE = datetime(2026, 9, 13, 6, 0, 0)
UNTIL = datetime(2026, 9, 14, 6, 0, 0)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _snapshot_data(*, ns=("ns1.example.com.",), expires=None):
    """Minimal shape diff() reads from: dns.records.NS, rdap.locked/expires/statuses, mail."""
    return {
        "dns": {"status": "ok", "error": None, "records": {"NS": {"ok": True, "values": list(ns)}}},
        "mail": {"status": "ok", "error": None, "flags": [], "spf": {"present": True}, "dmarc": {"present": True, "policy": "reject"}},
        "rdap": {
            "status": "ok",
            "error": None,
            "locked": True,
            "statuses": ["clientTransferProhibited"],
            "expires": expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
        },
        "data_hash": "deadbeef",
    }


def _add_domain(session, name="example.com", **kw):
    domain = Domain(name=name, source="manual", **kw)
    session.add(domain)
    session.commit()
    session.refresh(domain)
    return domain


def _add_snapshot(session, domain, taken_at, data):
    import json

    snap = DomainSnapshot(domain_id=domain.id, taken_at=taken_at, data=json.dumps(data), data_hash=data["data_hash"])
    session.add(snap)
    session.commit()
    session.refresh(snap)
    return snap


def test_no_domains_returns_ok_empty(temp_db):
    result = collect_domains(SINCE, UNTIL)
    assert isinstance(result, CollectionResult)
    assert result.source == "domains"
    assert result.status == "ok"
    assert result.items == []


def test_domain_with_no_snapshots_is_skipped(temp_db):
    with db.get_session() as session:
        _add_domain(session)
    result = collect_domains(SINCE, UNTIL)
    assert result.status == "ok"
    assert result.items == []


def test_ns_change_in_window_yields_domain_alert_item(temp_db):
    with db.get_session() as session:
        domain = _add_domain(session)
        _add_snapshot(session, domain, SINCE - timedelta(hours=1), _snapshot_data(ns=("ns1.example.com.",)))
        cur = _add_snapshot(session, domain, SINCE + timedelta(hours=1), _snapshot_data(ns=("ns2.example.com.",)))
        cur_id = cur.id

    result = collect_domains(SINCE, UNTIL)
    ns_items = [i for i in result.items if i.payload["kind"] == "ns_changed"]
    assert len(ns_items) == 1
    item = ns_items[0]
    assert isinstance(item, Item)
    assert item.item_type == "domain_alert"
    assert item.external_id == f"example.com:ns_changed:{cur_id}"
    assert item.payload["name"] == "example.com"
    assert "ns2.example.com." in item.payload["summary"]


def test_snapshot_before_window_is_not_diffed_as_an_alert(temp_db):
    """A change that happened entirely before `since` must not be re-reported this run."""
    with db.get_session() as session:
        domain = _add_domain(session)
        _add_snapshot(session, domain, SINCE - timedelta(hours=2), _snapshot_data(ns=("ns1.example.com.",)))
        _add_snapshot(session, domain, SINCE - timedelta(hours=1), _snapshot_data(ns=("ns2.example.com.",)))

    result = collect_domains(SINCE, UNTIL)
    assert not any(i.payload["kind"] == "ns_changed" for i in result.items)


def test_expiry_soon_fires_at_until_even_without_a_new_snapshot(temp_db):
    """expiry_soon is a function of `until`, not of new data — must fire off an old snapshot."""
    with db.get_session() as session:
        domain = _add_domain(session, auto_renew=False)
        old = _add_snapshot(
            session, domain, SINCE - timedelta(days=5), _snapshot_data(expires=UNTIL + timedelta(days=5))
        )
        old_id = old.id

    result = collect_domains(SINCE, UNTIL)
    expiry_items = [i for i in result.items if i.payload["kind"] == "expiry_soon"]
    assert len(expiry_items) == 1
    assert expiry_items[0].external_id == f"example.com:expiry_soon:{old_id}"


def test_no_alerts_when_nothing_changed_and_not_near_expiry(temp_db):
    with db.get_session() as session:
        domain = _add_domain(session, auto_renew=True)
        data = _snapshot_data(expires=UNTIL + timedelta(days=365))
        _add_snapshot(session, domain, SINCE - timedelta(hours=1), data)
        _add_snapshot(session, domain, SINCE + timedelta(hours=1), data)

    result = collect_domains(SINCE, UNTIL)
    assert result.items == []
    assert result.status == "ok"


def test_corrupt_snapshot_json_is_reported_as_partial_not_raised(temp_db):
    with db.get_session() as session:
        domain = _add_domain(session)
        assert domain.id is not None
        snap = DomainSnapshot(domain_id=domain.id, taken_at=SINCE + timedelta(hours=1), data="{not json", data_hash="x")
        session.add(snap)
        session.commit()

    result = collect_domains(SINCE, UNTIL)
    assert result.status == "partial"
    assert result.items == []
    assert "example.com" in result.detail
