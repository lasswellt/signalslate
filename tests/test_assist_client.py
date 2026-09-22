"""
Tests for pipeline.jobs.assist.client.AssistClient (T-025).

Drives the real FastAPI app (job_apply + jobs routers, install_security, a temp DB) through
Starlette's TestClient injected as AssistClient's `session` — same fixture shape as
tests/test_api_job_apply.py and tests/test_api_jobs.py, mounted together here since
fetch_application composes calls across both routers. No mocking of the HTTP layer itself.
"""
import json
import sys
from pathlib import Path

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import job_apply as job_apply_router  # noqa: E402
from api.routers import jobs as jobs_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.db import JobApplication, JobBoard, JobCompany, JobPosting  # noqa: E402
from pipeline.jobs.assist.client import AssistApiError, AssistClient, DEFAULT_BASE_URL  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def test_client(temp_db, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(job_apply_router, "DB_PATH", tmp_path / "digest.db")
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(job_apply_router.router, prefix="/api")
    app.include_router(jobs_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


@pytest.fixture
def client(test_client) -> AssistClient:
    return AssistClient(base_url="http://testserver", session=test_client)


def _insert_company(**overrides) -> JobCompany:
    fields = {"name": "Acme Corp", "domain": "acme.com", "source": "manual", "status": "active"}
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
    }
    fields.update(overrides)
    with db.get_session() as session:
        row = JobPosting(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _insert_application(posting_id: int, **overrides) -> JobApplication:
    fields = {"posting_id": posting_id}
    fields.update(overrides)
    with db.get_session() as session:
        row = JobApplication(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _posting_with_application(**app_overrides) -> tuple[JobPosting, JobApplication]:
    company = _insert_company()
    assert company.id is not None
    board = _insert_board(company.id)
    assert board.id is not None
    posting = _insert_posting(board.id)
    assert posting.id is not None
    application = _insert_application(posting.id, **app_overrides)
    return posting, application


# --- construction ------------------------------------------------------------------------------


def test_default_base_url_from_env(monkeypatch):
    monkeypatch.setenv("SIGNALSLATE_API_URL", "http://example.internal:9000/")
    assert AssistClient().base_url == "http://example.internal:9000"


def test_default_base_url_fallback(monkeypatch):
    monkeypatch.delenv("SIGNALSLATE_API_URL", raising=False)
    assert AssistClient().base_url == DEFAULT_BASE_URL


def test_default_session_is_requests_session(monkeypatch):
    monkeypatch.delenv("SIGNALSLATE_API_URL", raising=False)
    assert isinstance(AssistClient().session, requests.Session)


# --- next_queued ---------------------------------------------------------------------------------


def test_next_queued_empty(client):
    assert client.next_queued() is None


def test_next_queued_returns_oldest(client):
    _posting, application = _posting_with_application(status="ready", assist_state="queued")
    result = client.next_queued()
    assert result is not None
    assert result["id"] == application.id
    assert result["assist_state"] == "queued"


# --- claim ---------------------------------------------------------------------------------------


def test_claim_success(client):
    _posting, application = _posting_with_application(status="ready", assist_state="queued")
    result = client.claim(application.id)
    assert result["assist_state"] == "claimed"
    assert result["id"] == application.id


def test_claim_not_queued_raises(client):
    _posting, application = _posting_with_application(status="ready", assist_state="idle")
    with pytest.raises(AssistApiError) as excinfo:
        client.claim(application.id)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "not_queued"


def test_claim_missing_application_raises(client):
    with pytest.raises(AssistApiError) as excinfo:
        client.claim(999)
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "application_not_found"


def test_claim_response_includes_session_id(client):
    """ApplicationOut carries assist_session_id (fixed alongside this task) so the runner can learn
    its session id from claim()'s response instead of needing a second lookup."""
    _posting, application = _posting_with_application(status="ready", assist_state="queued")
    result = client.claim(application.id)
    assert result["assist_session_id"]


# --- fetch_application ---------------------------------------------------------------------------


def test_fetch_application_composes_all_three(client):
    posting, application = _posting_with_application(
        status="ready", packet=json.dumps({"cover_letter_text": "Dear Acme", "screening_drafts": []})
    )
    result = client.fetch_application(application.id)
    assert result["application"]["id"] == application.id
    assert result["application"]["packet"]["cover_letter_text"] == "Dear Acme"
    assert result["posting"]["id"] == posting.id
    assert result["posting"]["ats_kind"] == "greenhouse"
    assert result["profile"]["id"] == 1


def test_fetch_application_missing_raises_404(client):
    with pytest.raises(AssistApiError) as excinfo:
        client.fetch_application(999)
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "application_not_found"


# --- download_file -------------------------------------------------------------------------------


def test_download_file_missing_is_404(client):
    _posting, application = _posting_with_application()
    with pytest.raises(AssistApiError) as excinfo:
        client.download_file(application.id, "resume", "/tmp/should-not-be-written.pdf")
    assert excinfo.value.status_code == 404


def test_download_file_writes_bytes(client, tmp_path):
    _posting, application = _posting_with_application(resume_path=str(tmp_path / "src_resume.pdf"))
    (tmp_path / "src_resume.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    dest = tmp_path / "downloaded.pdf"
    result = client.download_file(application.id, "resume", str(dest))
    assert result == str(dest)
    assert dest.read_bytes() == b"%PDF-1.4\n%%EOF"


# --- report_progress -----------------------------------------------------------------------------


def test_report_progress_omits_unset_fields(client):
    _posting, application = _posting_with_application(
        status="ready", assist_state="claimed", assist_session_id="session-1"
    )
    session_id = application.assist_session_id

    result = client.report_progress(session_id, step="my_information")
    assert result["assist_state"] == "claimed"  # unset assist_state left untouched

    result = client.report_progress(session_id, assist_state="running")
    assert result["assist_state"] == "running"


def test_report_progress_filled_fields_and_answers(client):
    _posting, application = _posting_with_application(
        status="ready", assist_state="claimed", assist_session_id="session-2"
    )
    session_id = application.assist_session_id

    result = client.report_progress(
        session_id,
        step="my_information",
        filled_fields=[{"field_id": "email", "value": "a@b.com", "source": "profile", "confidence": 1.0}],
        approved_answers=[{"question": "Why us?", "answer": "Great mission"}],
    )
    assert result["id"] == application.id

    answers = _list_answers(client)
    assert any(a["answer"] == "Great mission" for a in answers)


def _list_answers(client: AssistClient) -> list[dict]:
    """Small helper: list answers via the underlying session, not part of AssistClient's contract."""
    return client._request("GET", "/api/jobs/answers").json()


def test_report_progress_unknown_state_raises(client):
    _posting, application = _posting_with_application(
        status="ready", assist_state="claimed", assist_session_id="session-3"
    )
    session_id = application.assist_session_id
    with pytest.raises(AssistApiError) as excinfo:
        client.report_progress(session_id, assist_state="bogus")
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "unknown_assist_state"


def test_report_progress_missing_session_raises_404(client):
    with pytest.raises(AssistApiError) as excinfo:
        client.report_progress("does-not-exist", step="x")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "session_not_found"
