"""
Tests for api.routers.packages: router-only app with the real CSRF guard, a temp DB, no network.
scan_all/refresh_due are monkeypatched at the router's own import site.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import packages as packages_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import Package, PackageEmail, PackageEvent  # noqa: E402
from pipeline.packages import inventory  # noqa: E402
from pipeline.packages.poll import PollResult  # noqa: E402
from pipeline.packages.scan import ScanResult  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
UPS = "1Z999AA10123456784"


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(temp_db, monkeypatch) -> TestClient:
    monkeypatch.setattr(packages_router, "schedule_timezone", lambda: None)
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(packages_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


def _add(**fields) -> int:
    fields.setdefault("carrier", "ups")
    with db.get_session() as session:
        row = Package(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_list_ordering_and_filters(client):
    now = utcnow()
    _add(tracking_number="D1", status="delivered", delivered_at=now - timedelta(days=3))
    _add(tracking_number="D2", status="delivered", delivered_at=now - timedelta(days=1))
    _add(tracking_number="P1", status="pending", merchant="Acme Store")
    _add(tracking_number="T1", status="in_transit", carrier="usps")
    _add(tracking_number="O1", status="out_for_delivery")
    _add(tracking_number="X1", status="exception")
    _add(tracking_number="A1", status="in_transit", archived=True)
    rows = client.get("/api/packages").json()
    assert [r["tracking_number"] for r in rows] == ["O1", "T1", "P1", "X1", "D2", "D1"]
    assert [r["tracking_number"] for r in client.get("/api/packages?status=pending").json()] == ["P1"]
    assert [r["tracking_number"] for r in client.get("/api/packages?carrier=usps").json()] == ["T1"]
    assert [r["tracking_number"] for r in client.get("/api/packages?q=acme").json()] == ["P1"]
    assert len(client.get("/api/packages?include_archived=true").json()) == 7


def test_summary_day_boundaries(client):
    now = utcnow()
    _add(tracking_number="O", status="out_for_delivery", eta=now)
    _add(tracking_number="E1", status="in_transit", eta=now)
    _add(tracking_number="E2", status="in_transit", eta=now - timedelta(days=1))
    _add(tracking_number="DT", status="delivered", delivered_at=now, eta=now)
    _add(tracking_number="DY", status="delivered", delivered_at=now - timedelta(days=1))
    _add(tracking_number="X", status="exception")
    _add(tracking_number="AR", status="in_transit", archived=True, eta=now)
    body = client.get("/api/packages/summary").json()
    assert body["out_for_delivery_today"] == 1
    assert body["arriving_today"] == 2
    assert body["delivered_today"] == 1
    assert body["in_transit"] == 2
    assert body["exceptions"] == 1
    assert body["generated_at"].endswith("Z")


def test_detail_events_emails_no_body(client):
    pid = _add(tracking_number=UPS, status="in_transit")
    now = utcnow()
    with db.get_session() as session:
        session.add(PackageEvent(package_id=pid, occurred_at=now - timedelta(hours=2), status="pending",
                                 description="Label created", source="email", dedupe_hash="h1"))
        session.add(PackageEvent(package_id=pid, occurred_at=now, status="in_transit",
                                 description="Departed facility", location="Louisville, KY",
                                 source="carrier", dedupe_hash="h2"))
        session.add(PackageEmail(package_id=pid, account_label="main", message_id="m1",
                                 subject="Your order shipped", sender="ship@acme.com", received_at=now))
        session.commit()
    body = client.get(f"/api/packages/{pid}").json()
    assert [e["description"] for e in body["events"]] == ["Departed facility", "Label created"]
    assert body["last_event"] == "Departed facility"
    assert body["emails"][0]["subject"] == "Your order shipped"
    assert "body" not in json.dumps(body).lower().replace("somebody", "")
    assert body["tracking_url"].startswith("https://")
    err = client.get("/api/packages/9999")
    assert err.status_code == 404 and err.json()["detail"]["code"] == "package_not_found"


def test_add_manual(client):
    resp = client.post("/api/packages", json={"tracking_number": UPS})
    assert resp.status_code == 201
    assert resp.json()["carrier"] == "ups" and resp.json()["status_source"] == "manual"
    dup = client.post("/api/packages", json={"tracking_number": UPS})
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "package_exists"
    bad = client.post("/api/packages", json={"tracking_number": "hello"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "unrecognized_tracking_number"


def test_archive_and_unarchive(client):
    pid = _add(tracking_number="A")
    assert client.patch(f"/api/packages/{pid}", json={"archived": True}).json()["archived"] is True
    assert client.get("/api/packages").json() == []
    assert client.patch(f"/api/packages/{pid}", json={"archived": False}).json()["archived"] is False
    assert len(client.get("/api/packages").json()) == 1
    assert client.patch("/api/packages/9999", json={"archived": True}).status_code == 404


def test_delete_cascades(client):
    pid = _add(tracking_number="A")
    with db.get_session() as session:
        session.add(PackageEvent(package_id=pid, occurred_at=utcnow(), status="pending", dedupe_hash="d"))
        session.add(PackageEmail(package_id=pid, account_label="m", message_id="1"))
        session.commit()
    assert client.delete(f"/api/packages/{pid}").status_code == 204
    with db.get_session() as session:
        assert session.exec(select(Package)).all() == []
        assert session.exec(select(PackageEvent)).all() == []
        assert session.exec(select(PackageEmail)).all() == []
    assert client.delete(f"/api/packages/{pid}").status_code == 404


def test_scan_and_refresh(client, monkeypatch):
    monkeypatch.setattr(packages_router.package_scan, "scan_all",
                        lambda: [ScanResult(label="main", scanned=3, packages_touched=2, errors=["X"])])
    monkeypatch.setattr(packages_router.poll, "refresh_due",
                        lambda: PollResult(polled=2, changed=1, errors={"ups": "CarrierAuthError"}))
    assert client.post("/api/packages/scan").json() == [
        {"label": "main", "scanned": 3, "packages_touched": 2, "errors": ["X"]}]
    assert client.post("/api/packages/refresh").json() == {
        "polled": 2, "changed": 1, "errors": {"ups": "CarrierAuthError"}}


def test_scan_refresh_failures_are_coded(client, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("secret-token-123")
    monkeypatch.setattr(packages_router.package_scan, "scan_all", boom)
    monkeypatch.setattr(packages_router.poll, "refresh_due", boom)
    for path, code in (("scan", "scan_failed"), ("refresh", "refresh_failed")):
        resp = client.post(f"/api/packages/{path}")
        assert resp.status_code == 502 and resp.json()["detail"]["code"] == code
        assert "secret-token-123" not in resp.text and "RuntimeError" in resp.text


def test_carriers_no_secrets(client, monkeypatch):
    monkeypatch.setenv("UPS_CLIENT_ID", "id-value-xyz")
    monkeypatch.setenv("UPS_CLIENT_SECRET", "secret-value-xyz")
    resp = client.get("/api/packages/carriers")
    assert resp.status_code == 200
    assert {c["code"] for c in resp.json()} == {"ups", "usps"}
    assert "xyz" not in resp.text


def test_write_guard(client):
    plain = TestClient(client.app)
    assert plain.post("/api/packages", json={"tracking_number": UPS}).status_code in (400, 403)
    assert plain.post("/api/packages/scan").status_code in (400, 403)
    assert plain.delete("/api/packages/1").status_code in (400, 403)


def test_summary_arriving_today_date_only_eta_west_of_utc(client, monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(packages_router, "schedule_timezone", lambda: ZoneInfo("America/New_York"))
    # local and UTC dates match (15:00 UTC = 11:00 EDT on Jun 10) / differ (02:00 UTC Jun 11 = Jun 10 EDT)
    for now in (datetime(2026, 6, 10, 15, 0), datetime(2026, 6, 11, 2, 0)):
        monkeypatch.setattr(packages_router, "utcnow", lambda now=now: now)
        with db.get_session() as session:
            for row in session.exec(select(Package)).all():
                session.delete(row)
            session.commit()
        _add(tracking_number="TODAY", status="in_transit", eta=datetime(2026, 6, 10))
        _add(tracking_number="TOMORROW", status="in_transit", eta=datetime(2026, 6, 11))
        assert client.get("/api/packages/summary").json()["arriving_today"] == 1


def test_add_archived_duplicate_message(client):
    pid = _add(tracking_number=UPS, status="in_transit", archived=True)
    dup = client.post("/api/packages", json={"tracking_number": UPS, "carrier": "ups"})
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "package_exists"
    assert dup.json()["detail"]["message"] == "Package already tracked (archived)"
    assert client.get(f"/api/packages/{pid}").json()["archived"] is True


def test_add_insert_race_is_409(client, monkeypatch):
    _add(tracking_number=UPS, status="pending")
    monkeypatch.setattr(inventory, "_find_numbered", lambda *_a, **_k: None)  # lookup misses; unique index fires
    resp = client.post("/api/packages", json={"tracking_number": UPS, "carrier": "ups"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "package_exists"
    assert len(client.get("/api/packages").json()) == 1


def test_add_input_bounds(client):
    for body in ({"tracking_number": "abc"}, {"tracking_number": "A" * 65},
                 {"tracking_number": "1Z99;DROP"}, {"tracking_number": UPS, "carrier": "c" * 33}):
        assert client.post("/api/packages", json=body).status_code == 422
    assert client.get("/api/packages", params={"q": "x" * 101}).status_code == 422
    assert client.get("/api/packages", params={"q": "x" * 100}).status_code == 200


def test_list_last_event_latest_per_package(client):
    now = utcnow()
    a = _add(tracking_number="A", status="in_transit")
    b = _add(tracking_number="B", status="in_transit")
    with db.get_session() as session:
        session.add(PackageEvent(package_id=a, occurred_at=now - timedelta(hours=1), status="pending",
                                 description="old", source="email", dedupe_hash="a1"))
        session.add(PackageEvent(package_id=a, occurred_at=now, status="in_transit",
                                 description="new", source="carrier", dedupe_hash="a2"))
        session.add(PackageEvent(package_id=b, occurred_at=now, status="in_transit",
                                 description="b-only", source="carrier", dedupe_hash="b1"))
        session.commit()
    rows = {r["tracking_number"]: r["last_event"] for r in client.get("/api/packages").json()}
    assert rows == {"A": "new", "B": "b-only"}
