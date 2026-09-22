"""
Tests for T-024's routes in api.routers.job_apply: the assist queue/claim/progress and
cover-letter/resume file-download endpoints for the desktop runner.

Same router-only app + temp-DB fixtures as tests/test_api_job_apply.py; duplicated here rather than
imported since pytest test modules are not meant to be import targets for each other.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import job_apply as job_apply_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.db import AnswerBank, JobApplication, JobBoard, JobCompany, JobPosting  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

_MINIMAL_PDF = b"%PDF-1.4\n%%EOF"


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(job_apply_router, "DB_PATH", tmp_path / "digest.db")
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(job_apply_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


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


def _another_posting_with_application(**app_overrides) -> tuple[JobPosting, JobApplication]:
    company = _insert_company(name="Beta Inc", domain="beta.com")
    assert company.id is not None
    board = _insert_board(company.id, board_id="beta")
    assert board.id is not None
    posting = _insert_posting(board.id, external_id="2", apply_url="https://beta.com/jobs/2", content_hash="hash2")
    assert posting.id is not None
    application = _insert_application(posting.id, **app_overrides)
    return posting, application


# --- POST /jobs/applications/{id}/assist -----------------------------------------------------


def test_queue_assist_requires_ready_status(client):
    _posting, application = _posting_with_application(status="saved")
    resp = client.post(f"/api/jobs/applications/{application.id}/assist")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "not_ready"


def test_queue_assist_success_sets_queued(client):
    _posting, application = _posting_with_application(status="ready")
    resp = client.post(f"/api/jobs/applications/{application.id}/assist")
    assert resp.status_code == 200
    assert resp.json()["assist_state"] == "queued"


def test_queue_assist_not_found_is_404(client):
    resp = client.post("/api/jobs/applications/999/assist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "application_not_found"


def test_queue_assist_already_active_is_409(client):
    _posting, application = _posting_with_application(status="ready", assist_state="running")
    resp = client.post(f"/api/jobs/applications/{application.id}/assist")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "assist_already_active"


# --- GET /jobs/assist/queue --------------------------------------------------------------------


def test_get_assist_queue_returns_oldest_by_id(client):
    _posting1, application1 = _posting_with_application(status="ready", assist_state="queued")
    _posting2, application2 = _another_posting_with_application(status="ready", assist_state="queued")
    assert application1.id < application2.id

    resp = client.get("/api/jobs/assist/queue")
    assert resp.status_code == 200
    assert resp.json()["id"] == application1.id


def test_get_assist_queue_empty_returns_null(client):
    resp = client.get("/api/jobs/assist/queue")
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_assist_queue_ignores_non_queued(client):
    _posting, _application = _posting_with_application(status="ready", assist_state="claimed")
    resp = client.get("/api/jobs/assist/queue")
    assert resp.json() is None


# --- POST /jobs/assist/queue/{id}/claim ---------------------------------------------------------


def test_claim_assist_success_sets_claimed_and_session_id(client):
    _posting, application = _posting_with_application(status="ready", assist_state="queued")
    resp = client.post(f"/api/jobs/assist/queue/{application.id}/claim")
    assert resp.status_code == 200
    body = resp.json()
    assert body["assist_state"] == "claimed"

    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        assert row.assist_session_id is not None
        assert len(row.assist_session_id) == 32  # uuid4().hex


def test_claim_assist_second_claim_is_409(client):
    _posting, application = _posting_with_application(status="ready", assist_state="queued")
    resp1 = client.post(f"/api/jobs/assist/queue/{application.id}/claim")
    assert resp1.status_code == 200

    resp2 = client.post(f"/api/jobs/assist/queue/{application.id}/claim")
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["code"] == "not_queued"


def test_claim_assist_not_found_is_404(client):
    resp = client.post("/api/jobs/assist/queue/999/claim")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "application_not_found"


def test_claim_assist_not_queued_is_409(client):
    _posting, application = _posting_with_application(status="ready", assist_state="idle")
    resp = client.post(f"/api/jobs/assist/queue/{application.id}/claim")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_queued"


# --- PATCH /jobs/assist/sessions/{session_id} -----------------------------------------------------


def _claimed_application(**overrides) -> tuple[JobApplication, str]:
    _posting, application = _posting_with_application(status="ready", assist_state="queued", **overrides)
    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        row.assist_state = "claimed"
        row.assist_session_id = "session-abc"
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row, "session-abc"


def test_patch_assist_session_updates_state_and_step(client):
    application, session_id = _claimed_application()
    resp = client.patch(
        f"/api/jobs/assist/sessions/{session_id}",
        json={"assist_state": "running", "step": "my_information"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["assist_state"] == "running"

    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        log = json.loads(row.assist_log)
        assert log == [{"step": "my_information"}]


def test_patch_assist_session_appends_filled_fields_without_replacing(client):
    application, session_id = _claimed_application()

    resp1 = client.patch(
        f"/api/jobs/assist/sessions/{session_id}",
        json={
            "step": "my_information",
            "filled_fields": [
                {"field_id": "email", "value": "ada@example.com", "source": "profile", "confidence": 1.0}
            ],
        },
    )
    assert resp1.status_code == 200

    resp2 = client.patch(
        f"/api/jobs/assist/sessions/{session_id}",
        json={
            "step": "my_experience",
            "filled_fields": [
                {"field_id": "phone", "value": "555-1234", "source": "profile", "confidence": 0.9}
            ],
        },
    )
    assert resp2.status_code == 200

    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        log = json.loads(row.assist_log)
        assert len(log) == 2
        assert log[0] == {"field_id": "email", "value": "ada@example.com", "source": "profile", "confidence": 1.0, "step": "my_information"}
        assert log[1]["field_id"] == "phone"
        assert log[1]["step"] == "my_experience"


def test_patch_assist_session_upserts_approved_answers(client):
    application, session_id = _claimed_application()
    resp = client.patch(
        f"/api/jobs/assist/sessions/{session_id}",
        json={"approved_answers": [{"question": "Why do you want to work here?", "answer": "Mission fit."}]},
    )
    assert resp.status_code == 200

    with db.get_session() as session:
        rows = session.exec(select(AnswerBank)).all()
        assert len(rows) == 1
        assert rows[0].answer == "Mission fit."
        assert rows[0].question_norm == "why do you want to work here"
        assert rows[0].source_application_id == application.id


def test_patch_assist_session_rejects_unknown_assist_state(client):
    _application, session_id = _claimed_application()
    resp = client.patch(f"/api/jobs/assist/sessions/{session_id}", json={"assist_state": "bogus"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_assist_state"


def test_patch_assist_session_rejects_status_field(client):
    _application, session_id = _claimed_application()
    resp = client.patch(f"/api/jobs/assist/sessions/{session_id}", json={"status": "submitted"})
    assert resp.status_code == 422


def test_patch_assist_session_not_found_is_404(client):
    resp = client.patch("/api/jobs/assist/sessions/does-not-exist", json={"step": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "session_not_found"


def test_patch_assist_session_rejects_unknown_field(client):
    _application, session_id = _claimed_application()
    resp = client.patch(f"/api/jobs/assist/sessions/{session_id}", json={"step": "x", "extra": "nope"})
    assert resp.status_code == 422


# --- GET /jobs/applications/{id}/files/{kind} -----------------------------------------------------


def test_get_application_file_cover_letter_success(client, tmp_path):
    _posting, application = _posting_with_application()
    pdf_path = tmp_path / "cover.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        row.cover_letter_path = str(pdf_path)
        session.add(row)
        session.commit()

    resp = client.get(f"/api/jobs/applications/{application.id}/files/cover_letter")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _MINIMAL_PDF


def test_get_application_file_resume_success(client, tmp_path):
    _posting, application = _posting_with_application()
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        row.resume_path = str(pdf_path)
        session.add(row)
        session.commit()

    resp = client.get(f"/api/jobs/applications/{application.id}/files/resume")
    assert resp.status_code == 200
    assert resp.content == _MINIMAL_PDF


def test_get_application_file_not_set_is_404(client):
    _posting, application = _posting_with_application()
    resp = client.get(f"/api/jobs/applications/{application.id}/files/resume")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "file_not_set"


def test_get_application_file_missing_on_disk_is_404(client, tmp_path):
    _posting, application = _posting_with_application()
    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        row.cover_letter_path = str(tmp_path / "does-not-exist.pdf")
        session.add(row)
        session.commit()

    resp = client.get(f"/api/jobs/applications/{application.id}/files/cover_letter")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "file_missing"


def test_get_application_file_application_not_found_is_404(client):
    resp = client.get("/api/jobs/applications/999/files/resume")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "application_not_found"


def test_get_application_file_invalid_kind_is_422(client):
    _posting, application = _posting_with_application()
    resp = client.get(f"/api/jobs/applications/{application.id}/files/bogus")
    assert resp.status_code == 422
