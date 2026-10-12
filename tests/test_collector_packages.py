"""Tests for pipeline.collectors.packages.collect_packages(): local DB only, temp SQLite engine."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.collectors import CollectionResult, dispatch  # noqa: E402
from pipeline.collectors.packages import collect_packages  # noqa: E402
from pipeline.db import Package  # noqa: E402

SINCE = datetime(2026, 10, 9, 6, 0, 0)
UNTIL = datetime(2026, 10, 10, 6, 0, 0)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _add(**kw) -> int:
    kw.setdefault("carrier", "ups")
    with db.get_session() as session:
        pkg = Package(**kw)
        session.add(pkg)
        session.commit()
        session.refresh(pkg)
        return pkg.id


def test_empty_db_ok(temp_db):
    result = collect_packages(SINCE, UNTIL)
    assert isinstance(result, CollectionResult)
    assert (result.source, result.status, result.items) == ("packages", "ok", [])


def test_delivered_in_window(temp_db):
    pid = _add(tracking_number="1Z999AA10123456784", status="delivered", delivered_at=SINCE + timedelta(hours=3))
    (item,) = collect_packages(SINCE, UNTIL).items
    assert item.item_type == "package"
    assert item.external_id == f"package:{pid}:delivered"
    assert item.payload["tracking_number"] == "1Z999AA10123456784"
    assert item.payload["delivered_at"] == (SINCE + timedelta(hours=3)).isoformat()
    assert item.payload["tracking_url"]


def test_out_for_delivery_in_window(temp_db):
    pid = _add(tracking_number="1Z999AA10123456784", status="out_for_delivery", last_event_at=UNTIL - timedelta(hours=1))
    (item,) = collect_packages(SINCE, UNTIL).items
    assert item.external_id == f"package:{pid}:out_for_delivery"


def test_arriving_today(temp_db):
    pid = _add(tracking_number="1Z999AA10123456784", status="in_transit", eta=datetime(2026, 10, 10, 20, 0))
    (item,) = collect_packages(SINCE, UNTIL).items
    assert item.external_id == f"package:{pid}:in_transit"
    assert item.payload["eta"] == "2026-10-10T20:00:00"


def test_archived_excluded(temp_db):
    _add(tracking_number="1Z999AA10123456784", status="delivered", delivered_at=SINCE + timedelta(hours=1), archived=True)
    assert collect_packages(SINCE, UNTIL).items == []


def test_outside_window_excluded(temp_db):
    _add(tracking_number="1Z999AA10123456784", status="delivered", delivered_at=SINCE - timedelta(days=2))
    _add(tracking_number="1Z999AA10123456785", status="delivered", delivered_at=UNTIL)
    _add(tracking_number="1Z999AA10123456786", status="in_transit", eta=datetime(2026, 10, 12, 12, 0))
    _add(tracking_number="1Z999AA10123456787", status="out_for_delivery", last_event_at=SINCE - timedelta(days=1))
    assert collect_packages(SINCE, UNTIL).items == []


def test_external_id_stable_across_runs_and_changes_with_status(temp_db):
    pid = _add(tracking_number="1Z999AA10123456784", status="out_for_delivery", last_event_at=SINCE + timedelta(hours=1),
               eta=datetime(2026, 10, 10, 18, 0))
    first = collect_packages(SINCE, UNTIL).items
    second = collect_packages(SINCE, UNTIL).items
    assert [i.external_id for i in first] == [i.external_id for i in second] == [f"package:{pid}:out_for_delivery"]


def test_dispatch_routes_packages(temp_db):
    assert dispatch("packages", SINCE, UNTIL).source == "packages"
