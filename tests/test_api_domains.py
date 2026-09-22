"""
Tests for api.routers.domains.

Router-only app with install_security (the real CSRF/Origin guard), a temp DB and no network:
inventory/snapshot/intel functions are monkeypatched at this router module's own import site
(domains_router.inventory.*, domains_router.domain_snapshot.inspect, domains_router.domain_intel.*)
rather than mocking pipeline.domains.inventory/snapshot/intel themselves.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import domains as domains_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import Domain, DomainSnapshot  # noqa: E402
from pipeline.domains.inventory import ImportResult, SnapshotStatus, SyncStatus  # noqa: E402
from pipeline.domains.intel import ArchivedUrlsResult, SubdomainsResult  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}


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
    app.include_router(domains_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


def _insert_domain(**overrides) -> Domain:
    fields = {
        "name": "example.com",
        "ownership": "owned",
        "source": "manual",
        "first_seen": utcnow(),
        "last_seen": utcnow(),
    }
    fields.update(overrides)
    with db.get_session() as session:
        row = Domain(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _insert_snapshot(domain_id: int, *, data: dict, taken_at=None) -> DomainSnapshot:
    with db.get_session() as session:
        row = DomainSnapshot(
            domain_id=domain_id,
            taken_at=taken_at or utcnow(),
            data=json.dumps(data, sort_keys=True),
            data_hash="hash-" + str(taken_at),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


# --- CSRF -------------------------------------------------------------------------------------


def test_post_without_csrf_header_is_403(client):
    resp = client.post("/api/domains", json={"name": "example.com"}, headers={"X-Requested-With": ""})
    assert resp.status_code == 403


# --- POST /domains (manual add) ----------------------------------------------------------------


def test_add_domain_creates_row(client):
    resp = client.post("/api/domains", json={"name": "Example.com", "ownership": "watched"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "example.com"
    assert body["ownership"] == "watched"
    assert body["source"] == "manual"
    assert body["missing_since"] is None


def test_add_domain_invalid_name_is_422(client):
    resp = client.post("/api/domains", json={"name": "not a domain"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_domain"


def test_add_domain_rejects_unknown_field(client):
    resp = client.post("/api/domains", json={"name": "example.com", "extra": "nope"})
    assert resp.status_code == 422


def test_add_domain_rejects_bad_ownership(client):
    resp = client.post("/api/domains", json={"name": "example.com", "ownership": "bogus"})
    assert resp.status_code == 422


# --- GET /domains -------------------------------------------------------------------------------


def test_list_domains_filters_by_ownership_and_source(client):
    _insert_domain(name="owned.example", ownership="owned", source="manual")
    _insert_domain(name="watched.example", ownership="watched", source="manual")
    _insert_domain(name="reg.example", ownership="owned", source="namecheap")

    resp = client.get("/api/domains")
    assert resp.status_code == 200
    assert {row["name"] for row in resp.json()} == {"owned.example", "watched.example", "reg.example"}

    resp = client.get("/api/domains", params={"ownership": "watched"})
    assert [row["name"] for row in resp.json()] == ["watched.example"]

    resp = client.get("/api/domains", params={"source": "namecheap"})
    assert [row["name"] for row in resp.json()] == ["reg.example"]


# --- GET /domains/{name} -------------------------------------------------------------------------


def test_get_domain_not_found(client):
    resp = client.get("/api/domains/nowhere.example")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "domain_not_found"


def test_get_domain_invalid_name_is_404(client):
    resp = client.get("/api/domains/not a domain")
    assert resp.status_code == 404


def test_get_domain_returns_latest_and_history(client):
    row = _insert_domain(name="example.com")
    from datetime import timedelta

    assert row.id is not None
    older = utcnow() - timedelta(days=1)
    newer = utcnow()
    _insert_snapshot(row.id, data={"dns": {"status": "ok"}}, taken_at=older)
    _insert_snapshot(row.id, data={"dns": {"status": "changed"}}, taken_at=newer)

    resp = client.get("/api/domains/example.com")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "example.com"
    assert body["latest"]["dns"]["status"] == "changed"
    assert len(body["history"]) == 2


# --- DELETE /domains/{name} -----------------------------------------------------------------------


def test_delete_manual_domain_marks_missing(client):
    _insert_domain(name="example.com", ownership="owned", source="manual")
    resp = client.delete("/api/domains/example.com")
    assert resp.status_code == 204

    resp = client.get("/api/domains", params={"source": "manual"})
    row = resp.json()[0]
    assert row["missing_since"] is not None

    # already missing -> 404 on a second delete
    resp = client.delete("/api/domains/example.com")
    assert resp.status_code == 404


def test_delete_watched_registrar_domain_allowed(client):
    _insert_domain(name="example.com", ownership="watched", source="namecheap", connection_id=None)
    resp = client.delete("/api/domains/example.com")
    assert resp.status_code == 204


def test_delete_registrar_owned_domain_refused(client):
    _insert_domain(name="example.com", ownership="owned", source="namecheap", connection_id=None)
    resp = client.delete("/api/domains/example.com")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "registrar_owned"


def test_delete_unknown_domain_is_404(client):
    resp = client.delete("/api/domains/nowhere.example")
    assert resp.status_code == 404


# --- POST /domains/import -------------------------------------------------------------------------


def test_import_csv_adds_and_rejects_rows(client):
    csv_text = "good.example\nwatched.example,watched\nnot a domain\n"
    resp = client.post("/api/domains/import", json={"csv": csv_text})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["added"]) == {"good.example", "watched.example"}
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["line"] == 3


def test_import_csv_too_large_is_422(client):
    resp = client.post("/api/domains/import", json={"csv": "a" * (domains_router._MAX_IMPORT_CHARS + 1)})
    assert resp.status_code == 422


# --- POST /domains/sync ----------------------------------------------------------------------------


def test_sync_domains_calls_sync_then_refresh(client, monkeypatch):
    sync_calls = []
    refresh_calls = []

    def fake_sync_all():
        sync_calls.append(True)
        return [SyncStatus(connection_id="c1", kind="namecheap", status="ok", detail="synced 1 domain(s)", domain_count=1)]

    def fake_refresh_snapshots():
        refresh_calls.append(True)
        return [SnapshotStatus(name="example.com", status="stored", detail="snapshot stored")]

    monkeypatch.setattr(domains_router.inventory, "sync_all", fake_sync_all)
    monkeypatch.setattr(domains_router.inventory, "refresh_snapshots", fake_refresh_snapshots)

    resp = client.post("/api/domains/sync", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sync"] == [{"connection_id": "c1", "kind": "namecheap", "status": "ok", "detail": "synced 1 domain(s)", "domain_count": 1}]
    assert body["refresh"] == [{"name": "example.com", "status": "stored", "detail": "snapshot stored"}]
    assert sync_calls and refresh_calls


# --- POST /domains/inspect --------------------------------------------------------------------------


def _fake_inspect(name):
    return {
        "dns": {"status": "ok", "error": None, "records": {}},
        "mail": {"status": "ok", "error": None},
        "rdap": {"status": "ok"},
        "data_hash": "deadbeef",
    }


def test_inspect_domain_without_intel_does_not_persist(client, monkeypatch):
    monkeypatch.setattr(domains_router.domain_snapshot, "inspect", _fake_inspect)

    resp = client.post("/api/domains/inspect", json={"name": "example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "example.com"
    assert body["data_hash"] == "deadbeef"
    assert body["intel"] is None

    with db.get_session() as session:
        assert session.exec(__import__("sqlmodel").select(DomainSnapshot)).all() == []


def test_inspect_domain_with_intel(client, monkeypatch):
    monkeypatch.setattr(domains_router.domain_snapshot, "inspect", _fake_inspect)
    monkeypatch.setattr(
        domains_router.domain_intel,
        "subdomains",
        lambda name: SubdomainsResult(status="ok", names=("www.example.com",), error=None),
    )
    monkeypatch.setattr(
        domains_router.domain_intel,
        "archived_urls",
        lambda name: ArchivedUrlsResult(status="unavailable", urls=(), error="Timeout"),
    )

    resp = client.post("/api/domains/inspect", json={"name": "example.com", "intel": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intel"]["subdomains"] == {"status": "ok", "names": ["www.example.com"], "error": None}
    assert body["intel"]["archived_urls"] == {"status": "unavailable", "urls": [], "error": "Timeout"}


def test_inspect_domain_invalid_name_is_422(client):
    resp = client.post("/api/domains/inspect", json={"name": "not a domain"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_domain"


def test_inspect_domain_rejects_unknown_field(client):
    resp = client.post("/api/domains/inspect", json={"name": "example.com", "nope": 1})
    assert resp.status_code == 422
