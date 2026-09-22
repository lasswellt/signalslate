"""
Tests for api.routers.jobs.

Router-only app with install_security (the real CSRF/Origin guard), a temp DB and no network:
pipeline.jobs.seeds/resolve/research/ats are monkeypatched at this router module's own import site
(jobs_router.seeds.*, jobs_router.resolve.resolve_company, jobs_router.research.research_company,
jobs_router.ats.get_adapter) rather than mocking pipeline.jobs.* themselves. Endpoints with no
network dependency (add/list companies, CSV import, board override, postings list/filter) exercise
the real pipeline.jobs.seeds/inventory logic against the temp DB.
"""
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import jobs as jobs_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import JobBoard, JobCompany, JobPosting  # noqa: E402
from pipeline.jobs.ats import RawPosting  # noqa: E402
from pipeline.jobs.resolve import ResolveResult  # noqa: E402
from pipeline.jobs.seeds import ImportResult  # noqa: E402

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
    app.include_router(jobs_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


def _insert_company(**overrides) -> JobCompany:
    fields = {
        "name": "Acme Corp",
        "domain": "acme.com",
        "source": "manual",
        "status": "active",
        "first_seen": utcnow(),
        "last_seen": utcnow(),
    }
    fields.update(overrides)
    with db.get_session() as session:
        row = JobCompany(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _insert_board(company_id: int, **overrides) -> JobBoard:
    fields = {
        "company_id": company_id,
        "ats_kind": "greenhouse",
        "board_id": "acme",
        "resolved_by": "pattern",
        "confidence": 1.0,
    }
    fields.update(overrides)
    with db.get_session() as session:
        row = JobBoard(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _insert_posting(board_id: int, **overrides) -> JobPosting:
    fields = {
        "board_id": board_id,
        "external_id": "1",
        "title": "Senior Engineer",
        "apply_url": "https://acme.com/jobs/1",
        "content_hash": "hash1",
        "first_seen": utcnow(),
        "last_seen": utcnow(),
    }
    fields.update(overrides)
    with db.get_session() as session:
        row = JobPosting(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


class _FakeAdapter:
    def __init__(self, postings=None, error=None):
        self._postings = postings or []
        self._error = error

    def list_postings(self, board_id):
        if self._error:
            raise self._error
        return self._postings


# --- CSRF -----------------------------------------------------------------------------------


def test_post_without_csrf_header_is_403(client):
    resp = client.post("/api/jobs/companies", json={"name": "Acme"}, headers={"X-Requested-With": ""})
    assert resp.status_code == 403


# --- POST /jobs/companies ---------------------------------------------------------------------


def test_add_company_creates_row(client):
    resp = client.post("/api/jobs/companies", json={"name": "Acme Corp", "domain": "acme.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Acme Corp"
    assert body["domain"] == "acme.com"
    assert body["source"] == "manual"
    assert body["status"] == "active"
    assert body["boards"] == []


def test_add_company_rejects_unknown_field(client):
    resp = client.post("/api/jobs/companies", json={"name": "Acme", "extra": "nope"})
    assert resp.status_code == 422


def test_add_company_empty_name_is_422(client):
    resp = client.post("/api/jobs/companies", json={"name": "   "})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_company"


# --- GET /jobs/companies ----------------------------------------------------------------------


def test_list_companies_filters_by_status(client):
    _insert_company(name="Active Co", status="active")
    _insert_company(name="Muted Co", status="muted")

    resp = client.get("/api/jobs/companies")
    assert resp.status_code == 200
    assert {row["name"] for row in resp.json()} == {"Active Co", "Muted Co"}

    resp = client.get("/api/jobs/companies", params={"status": "muted"})
    assert [row["name"] for row in resp.json()] == ["Muted Co"]


def test_list_companies_includes_boards(client):
    company = _insert_company(name="Acme Corp")
    assert company.id is not None
    _insert_board(company.id, ats_kind="greenhouse", board_id="acme")

    resp = client.get("/api/jobs/companies")
    body = resp.json()[0]
    assert len(body["boards"]) == 1
    assert body["boards"][0]["ats_kind"] == "greenhouse"


# --- POST /jobs/companies/import ---------------------------------------------------------------


def test_import_companies_csv(client):
    resp = client.post("/api/jobs/companies/import", json={"csv": "Acme Corp,acme.com\nBad,,extra"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] == ["Acme Corp"]
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["line"] == 2


def test_import_companies_rejects_unknown_field(client):
    resp = client.post("/api/jobs/companies/import", json={"csv": "a", "extra": "nope"})
    assert resp.status_code == 422


# --- POST /jobs/companies/import-yc / import-hn -------------------------------------------------


def test_import_yc_uses_seeds_module(client, monkeypatch):
    def fake_import_yc(session):
        return ImportResult(added=["YC Co"], rejected=[])

    monkeypatch.setattr(jobs_router.seeds, "import_yc", fake_import_yc)
    resp = client.post("/api/jobs/companies/import-yc")
    assert resp.status_code == 200
    assert resp.json()["added"] == ["YC Co"]


def test_import_hn_uses_seeds_module(client, monkeypatch):
    def fake_import_hn(session):
        return ImportResult(added=["HN Co"], rejected=[])

    monkeypatch.setattr(jobs_router.seeds, "import_hn", fake_import_hn)
    resp = client.post("/api/jobs/companies/import-hn")
    assert resp.status_code == 200
    assert resp.json()["added"] == ["HN Co"]


# --- POST /jobs/companies/import-inbox -----------------------------------------------------------


def test_import_inbox_uses_inbox_seed_module(client, monkeypatch):
    captured = {}

    def fake_seed_from_inbox(session, since, until):
        captured["since"] = since
        captured["until"] = until
        return ImportResult(added=["Inbox Co"], rejected=[])

    monkeypatch.setattr(jobs_router.inbox_seed, "seed_from_inbox", fake_seed_from_inbox)
    resp = client.post("/api/jobs/companies/import-inbox")
    assert resp.status_code == 200
    assert resp.json()["added"] == ["Inbox Co"]
    assert captured["since"] < captured["until"]


# --- PUT /jobs/boards/{id} ---------------------------------------------------------------------


def test_override_board_sets_manual_fields(client):
    company = _insert_company()
    assert company.id is not None
    board = _insert_board(company.id, ats_kind="lever", board_id="wrong", resolved_by="slug_probe", confidence=0.7)
    assert board.id is not None

    resp = client.put(f"/api/jobs/boards/{board.id}", json={"ats_kind": "greenhouse", "board_id": "acme"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ats_kind"] == "greenhouse"
    assert body["board_id"] == "acme"
    assert body["resolved_by"] == "manual"
    assert body["confidence"] == 1.0
    assert body["verified_at"] is not None


def test_override_board_not_found_is_404(client):
    resp = client.put("/api/jobs/boards/999", json={"ats_kind": "greenhouse", "board_id": "acme"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "board_not_found"


def test_override_board_rejects_unknown_field(client):
    company = _insert_company()
    assert company.id is not None
    board = _insert_board(company.id)
    resp = client.put(f"/api/jobs/boards/{board.id}", json={"ats_kind": "g", "board_id": "b", "extra": "nope"})
    assert resp.status_code == 422


# --- POST /jobs/companies/{id}/rescan -----------------------------------------------------------


def test_rescan_not_found_is_404(client):
    resp = client.post("/api/jobs/companies/999/rescan")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "company_not_found"


def test_rescan_resolves_via_resolve_company(client, monkeypatch):
    company = _insert_company(name="Acme Corp", domain="acme.com")
    assert company.id is not None

    def fake_resolve_company(company_arg):
        assert company_arg.name == "Acme Corp"
        return ResolveResult(ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0)

    monkeypatch.setattr(jobs_router.resolve, "resolve_company", fake_resolve_company)
    monkeypatch.setattr(
        jobs_router.ats, "get_adapter",
        lambda kind: _FakeAdapter([RawPosting(external_id="1", title="Engineer", url="https://acme.com/1")]),
    )

    resp = client.post(f"/api/jobs/companies/{company.id}/rescan")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["boards"]) == 1
    assert body["boards"][0]["ats_kind"] == "greenhouse"
    assert body["boards"][0]["resolved_by"] == "pattern"
    assert body["boards"][0]["last_polled_at"] is not None

    resp = client.get("/api/jobs/postings")
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Engineer"


def test_rescan_falls_through_to_research_on_resolve_miss(client, monkeypatch):
    company = _insert_company(name="Acme Corp", domain="acme.com")
    assert company.id is not None

    monkeypatch.setattr(jobs_router.resolve, "resolve_company", lambda company_arg: None)

    def fake_research_company(name, domain, **kwargs):
        return ResolveResult(ats_kind="lever", board_id="acme", resolved_by="llm", confidence=0.8), ""

    monkeypatch.setattr(jobs_router.research, "research_company", fake_research_company)
    monkeypatch.setattr(jobs_router.ats, "get_adapter", lambda kind: _FakeAdapter([]))

    resp = client.post(f"/api/jobs/companies/{company.id}/rescan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["boards"][0]["ats_kind"] == "lever"
    assert body["boards"][0]["resolved_by"] == "llm"


def test_rescan_no_hit_leaves_company_unresolved(client, monkeypatch):
    company = _insert_company(name="Acme Corp", domain="acme.com")
    assert company.id is not None

    monkeypatch.setattr(jobs_router.resolve, "resolve_company", lambda company_arg: None)
    monkeypatch.setattr(jobs_router.research, "research_company", lambda name, domain, **kwargs: (None, ""))

    resp = client.post(f"/api/jobs/companies/{company.id}/rescan")
    assert resp.status_code == 200
    assert resp.json()["boards"] == []


def test_rescan_research_timeout_leaves_company_unresolved(client, monkeypatch):
    company = _insert_company(name="Acme Corp", domain="acme.com")
    assert company.id is not None

    monkeypatch.setattr(jobs_router, "_RESCAN_RESEARCH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jobs_router.resolve, "resolve_company", lambda company_arg: None)

    def slow_research(name, domain, **kwargs):
        time.sleep(1)
        return ResolveResult(ats_kind="lever", board_id="acme", resolved_by="llm", confidence=0.8), ""

    monkeypatch.setattr(jobs_router.research, "research_company", slow_research)

    resp = client.post(f"/api/jobs/companies/{company.id}/rescan")
    assert resp.status_code == 200
    assert resp.json()["boards"] == []


def test_rescan_polls_existing_board(client, monkeypatch):
    company = _insert_company(name="Acme Corp")
    assert company.id is not None
    board = _insert_board(company.id, ats_kind="greenhouse", board_id="acme")
    assert board.id is not None

    monkeypatch.setattr(
        jobs_router.ats, "get_adapter",
        lambda kind: _FakeAdapter([RawPosting(external_id="2", title="Designer", url="https://acme.com/2")]),
    )

    resp = client.post(f"/api/jobs/companies/{company.id}/rescan")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["boards"]) == 1
    assert body["boards"][0]["last_polled_at"] is not None

    resp = client.get("/api/jobs/postings")
    assert [row["title"] for row in resp.json()] == ["Designer"]


def test_rescan_adapter_failure_sets_last_error(client, monkeypatch):
    company = _insert_company(name="Acme Corp")
    assert company.id is not None
    board = _insert_board(company.id)
    assert board.id is not None

    monkeypatch.setattr(jobs_router.ats, "get_adapter", lambda kind: _FakeAdapter(error=RuntimeError("unavailable")))

    resp = client.post(f"/api/jobs/companies/{company.id}/rescan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["boards"][0]["last_error"] == "RuntimeError"


# --- GET /jobs/postings ------------------------------------------------------------------------


def test_list_postings_filters(client):
    company_a = _insert_company(name="Acme Corp")
    company_b = _insert_company(name="Beta Inc")
    assert company_a.id is not None and company_b.id is not None
    board_a = _insert_board(company_a.id, ats_kind="greenhouse", board_id="acme")
    board_b = _insert_board(company_b.id, ats_kind="lever", board_id="beta")
    assert board_a.id is not None and board_b.id is not None

    _insert_posting(board_a.id, external_id="1", title="Senior Backend Engineer", fit_score=80, remote=True)
    _insert_posting(board_a.id, external_id="2", title="Sales Rep", fit_score=20, remote=False, closed_at=utcnow())
    _insert_posting(board_b.id, external_id="1", title="Backend Developer", fit_score=90, remote=True)

    resp = client.get("/api/jobs/postings")
    assert len(resp.json()) == 3

    resp = client.get("/api/jobs/postings", params={"min_score": 50})
    assert {row["title"] for row in resp.json()} == {"Senior Backend Engineer", "Backend Developer"}

    resp = client.get("/api/jobs/postings", params={"status": "open"})
    assert {row["title"] for row in resp.json()} == {"Senior Backend Engineer", "Backend Developer"}

    resp = client.get("/api/jobs/postings", params={"status": "closed"})
    assert [row["title"] for row in resp.json()] == ["Sales Rep"]

    resp = client.get("/api/jobs/postings", params={"company": "Acme"})
    assert {row["title"] for row in resp.json()} == {"Senior Backend Engineer", "Sales Rep"}

    resp = client.get("/api/jobs/postings", params={"text": "Backend"})
    assert {row["title"] for row in resp.json()} == {"Senior Backend Engineer", "Backend Developer"}

    resp = client.get("/api/jobs/postings", params={"remote": True})
    assert {row["title"] for row in resp.json()} == {"Senior Backend Engineer", "Backend Developer"}

    resp = client.get("/api/jobs/postings", params={"company": "Nonexistent"})
    assert resp.json() == []
