"""
Tests for api.routers.job_apply.

Router-only app with install_security (the real CSRF/Origin guard) and a temp DB, mirroring
tests/test_api_jobs.py's fixtures. pipeline.jobs.apply/packet are monkeypatched at this router
module's own import site (job_apply_router.apply.transition, job_apply_router.packet.*) where a
test needs to control the outcome without touching the real state machine or LLM call; the resume
upload tests write into tmp_path via a monkeypatched job_apply_router.DB_PATH rather than the real
data/ dir. No real network/API calls anywhere in this file.
"""
import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import job_apply as job_apply_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.db import JobApplication, JobBoard, JobCompany, JobPosting, JobsProfile  # noqa: E402
from pipeline.jobs.packet import ScreeningDraft  # noqa: E402

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


class _FakeParsedOutput:
    def __init__(self, cover_letter_text, screening_drafts):
        self.cover_letter_text = cover_letter_text
        self.screening_drafts = [ScreeningDraft(question=q, answer=a) for q, a in screening_drafts]


class _FakeResponse:
    def __init__(self, parsed_output=None, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def parse(self, **kwargs):
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


# --- CSRF -----------------------------------------------------------------------------------


def test_put_without_csrf_header_is_403(client):
    resp = client.put("/api/jobs/profile", json={}, headers={"X-Requested-With": ""})
    assert resp.status_code == 403


# --- GET/PUT /jobs/profile --------------------------------------------------------------------


def test_get_profile_lazily_creates_singleton(client):
    resp = client.get("/api/jobs/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["full_name"] is None
    assert body["resume_paths"] == []
    assert body["target_roles"] == []
    assert body["eeo_answers"] == {}

    with db.get_session() as session:
        assert session.get(JobsProfile, 1) is not None


def test_put_profile_full_replace_upserts_singleton(client):
    resp = client.put(
        "/api/jobs/profile",
        json={
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "target_roles": ["Backend Engineer", "Staff Engineer"],
            "eeo_answers": {"veteran_status": "decline"},
            "salary_disclosure_policy": "range",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] == "Ada Lovelace"
    assert body["target_roles"] == ["Backend Engineer", "Staff Engineer"]
    assert body["eeo_answers"] == {"veteran_status": "decline"}
    assert body["salary_disclosure_policy"] == "range"
    assert body["updated_at"] is not None

    resp = client.get("/api/jobs/profile")
    assert resp.json()["full_name"] == "Ada Lovelace"


def test_put_profile_rejects_unknown_field(client):
    resp = client.put("/api/jobs/profile", json={"full_name": "Ada", "extra": "nope"})
    assert resp.status_code == 422


def test_put_profile_cannot_set_resume_paths(client):
    resp = client.put("/api/jobs/profile", json={"resume_paths": ["/etc/passwd"]})
    assert resp.status_code == 422


# --- POST /jobs/profile/resume ------------------------------------------------------------------


def test_upload_resume_stores_pdf_and_appends_path(client, tmp_path):
    content_b64 = base64.b64encode(_MINIMAL_PDF).decode()
    resp = client.post("/api/jobs/profile/resume", json={"filename": "../../etc/passwd.pdf", "content_b64": content_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["resume_paths"]) == 1
    stored_path = Path(body["resume_paths"][0])
    assert stored_path.exists()
    assert stored_path.parent == tmp_path / "jobs" / "resumes"
    # Never the client-supplied filename verbatim, and no path traversal out of resumes_dir.
    assert stored_path.name != "../../etc/passwd.pdf"
    assert stored_path.name.startswith("resume_") and stored_path.name.endswith(".pdf")
    assert stored_path.read_bytes() == _MINIMAL_PDF

    resp = client.get("/api/jobs/profile")
    assert resp.json()["resume_paths"] == body["resume_paths"]


def test_upload_resume_second_upload_appends(client):
    content_b64 = base64.b64encode(_MINIMAL_PDF).decode()
    client.post("/api/jobs/profile/resume", json={"filename": "a.pdf", "content_b64": content_b64})
    resp = client.post("/api/jobs/profile/resume", json={"filename": "b.pdf", "content_b64": content_b64})
    assert len(resp.json()["resume_paths"]) == 2


def test_upload_resume_rejects_invalid_base64(client):
    resp = client.post("/api/jobs/profile/resume", json={"filename": "a.pdf", "content_b64": "not-base64!!!"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_base64"


def test_upload_resume_rejects_non_pdf_content(client):
    content_b64 = base64.b64encode(b"just some text, not a pdf").decode()
    resp = client.post("/api/jobs/profile/resume", json={"filename": "a.pdf", "content_b64": content_b64})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "not_a_pdf"


def test_upload_resume_rejects_oversized_content(client, monkeypatch):
    monkeypatch.setattr(job_apply_router, "_RESUME_MAX_BYTES", 4)
    content_b64 = base64.b64encode(_MINIMAL_PDF).decode()
    resp = client.post("/api/jobs/profile/resume", json={"filename": "a.pdf", "content_b64": content_b64})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "resume_too_large"


def test_upload_resume_rejects_unknown_field(client):
    resp = client.post("/api/jobs/profile/resume", json={"filename": "a.pdf", "content_b64": "x", "extra": "nope"})
    assert resp.status_code == 422


# --- GET/POST /jobs/answers ---------------------------------------------------------------------


def test_create_answer_normalizes_question_and_always_inserts(client):
    resp = client.post("/api/jobs/answers", json={"question_raw": "Are You Authorized to Work?", "answer": "Yes."})
    assert resp.status_code == 201
    body = resp.json()
    assert body["question_norm"] == "are you authorized to work"
    assert body["question_raw"] == "Are You Authorized to Work?"
    assert body["source_application_id"] is None

    resp = client.post("/api/jobs/answers", json={"question_raw": "are you authorized to work", "answer": "Yes, indeed."})
    assert resp.status_code == 201

    resp = client.get("/api/jobs/answers")
    assert len(resp.json()) == 2


def test_list_answers_filters_by_q(client):
    client.post("/api/jobs/answers", json={"question_raw": "Why do you want to work here?", "answer": "Mission fit."})
    client.post("/api/jobs/answers", json={"question_raw": "Salary expectations?", "answer": "Market rate."})

    resp = client.get("/api/jobs/answers", params={"q": "Salary"})
    body = resp.json()
    assert len(body) == 1
    assert body[0]["question_raw"] == "Salary expectations?"


def test_create_answer_rejects_unknown_field(client):
    resp = client.post("/api/jobs/answers", json={"question_raw": "a", "answer": "b", "extra": "nope"})
    assert resp.status_code == 422


# --- GET/POST /jobs/applications -----------------------------------------------------------------


def test_create_application_from_posting(client):
    company = _insert_company()
    assert company.id is not None
    board = _insert_board(company.id)
    assert board.id is not None
    posting = _insert_posting(board.id)
    assert posting.id is not None

    resp = client.post("/api/jobs/applications", json={"posting_id": posting.id})
    assert resp.status_code == 201
    body = resp.json()
    assert body["posting_id"] == posting.id
    assert body["status"] == "saved"
    assert body["packet"] is None


def test_create_application_posting_not_found_is_404(client):
    resp = client.post("/api/jobs/applications", json={"posting_id": 999})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "posting_not_found"


def test_create_application_duplicate_posting_is_409(client):
    company = _insert_company()
    assert company.id is not None
    board = _insert_board(company.id)
    assert board.id is not None
    posting = _insert_posting(board.id)
    assert posting.id is not None

    client.post("/api/jobs/applications", json={"posting_id": posting.id})
    resp = client.post("/api/jobs/applications", json={"posting_id": posting.id})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "application_exists"


def test_list_applications_filters_by_status(client):
    posting, _application = _posting_with_application(status="saved")
    company2 = _insert_company(name="Beta Inc")
    assert company2.id is not None
    board2 = _insert_board(company2.id, board_id="beta")
    assert board2.id is not None
    posting2 = _insert_posting(board2.id, external_id="2")
    assert posting2.id is not None
    _insert_application(posting2.id, status="ready")

    resp = client.get("/api/jobs/applications")
    assert len(resp.json()) == 2

    resp = client.get("/api/jobs/applications", params={"status": "ready"})
    assert len(resp.json()) == 1
    assert resp.json()[0]["posting_id"] == posting2.id


def test_create_application_rejects_unknown_field(client):
    resp = client.post("/api/jobs/applications", json={"posting_id": 1, "extra": "nope"})
    assert resp.status_code == 422


# --- PATCH /jobs/applications/{id} ---------------------------------------------------------------


def test_patch_status_legal_transition(client):
    _posting, application = _posting_with_application(status="saved")
    assert application.id is not None

    resp = client.patch(f"/api/jobs/applications/{application.id}", json={"status": "preparing"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "preparing"
    assert resp.json()["submitted_at"] is None


def test_patch_status_to_submitted_stamps_submitted_at(client):
    _posting, application = _posting_with_application(status="ready")
    assert application.id is not None

    resp = client.patch(f"/api/jobs/applications/{application.id}", json={"status": "submitted"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"
    assert resp.json()["submitted_at"] is not None


def test_patch_status_illegal_transition_is_422(client):
    _posting, application = _posting_with_application(status="saved")
    assert application.id is not None

    resp = client.patch(f"/api/jobs/applications/{application.id}", json={"status": "submitted"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "illegal_transition"


def test_patch_status_not_found_is_404(client):
    resp = client.patch("/api/jobs/applications/999", json={"status": "preparing"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "application_not_found"


def test_patch_status_rejects_unknown_field(client):
    _posting, application = _posting_with_application(status="saved")
    resp = client.patch(f"/api/jobs/applications/{application.id}", json={"status": "preparing", "extra": "nope"})
    assert resp.status_code == 422


# --- POST /jobs/applications/{id}/packet ----------------------------------------------------------


def test_prepare_packet_not_found_is_404(client):
    resp = client.post("/api/jobs/applications/999/packet")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "application_not_found"


def test_prepare_packet_success_stores_packet_and_pdf(client, monkeypatch):
    _posting, application = _posting_with_application()
    assert application.id is not None
    with db.get_session() as session:
        session.add(JobsProfile(id=1, full_name="Ada Lovelace"))
        session.commit()

    response = _FakeResponse(
        parsed_output=_FakeParsedOutput(
            "Dear Hiring Manager, I would love to join Acme.",
            [("Why us?", "Mission fit.")],
        )
    )
    monkeypatch.setattr(job_apply_router.packet, "make_client", lambda: _FakeClient(response))

    resp = client.post(f"/api/jobs/applications/{application.id}/packet")
    assert resp.status_code == 200
    body = resp.json()
    assert body["packet"]["cover_letter_text"] == "Dear Hiring Manager, I would love to join Acme."
    assert body["cover_letter_path"] is not None
    assert Path(body["cover_letter_path"]).exists()


def test_prepare_packet_failure_reason_is_422(client):
    _posting, application = _posting_with_application()
    assert application.id is not None
    # No JobsProfile row -> prepare_packet() returns (False, "no profile configured").

    resp = client.post(f"/api/jobs/applications/{application.id}/packet")
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["code"] == "packet_failed"
    assert body["message"] == "no profile configured"


# --- PUT /jobs/applications/{id}/packet -----------------------------------------------------------


def test_edit_packet_updates_text_and_rerenders_pdf(client):
    _posting, application = _posting_with_application()
    assert application.id is not None
    with db.get_session() as session:
        row = session.get(JobApplication, application.id)
        row.packet = json.dumps({"cover_letter_text": "Old draft.", "screening_drafts": [{"question": "Q", "answer": "A", "source": "generated"}]})
        session.add(row)
        session.commit()

    resp = client.put(f"/api/jobs/applications/{application.id}/packet", json={"cover_letter_text": "Edited draft."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["packet"]["cover_letter_text"] == "Edited draft."
    assert body["packet"]["screening_drafts"] == [{"question": "Q", "answer": "A", "source": "generated"}]
    assert Path(body["cover_letter_path"]).exists()


def test_edit_packet_with_no_prior_packet_creates_one(client):
    _posting, application = _posting_with_application()
    assert application.id is not None

    resp = client.put(f"/api/jobs/applications/{application.id}/packet", json={"cover_letter_text": "First draft."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["packet"]["cover_letter_text"] == "First draft."
    assert body["packet"]["screening_drafts"] == []


def test_edit_packet_not_found_is_404(client):
    resp = client.put("/api/jobs/applications/999/packet", json={"cover_letter_text": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "application_not_found"


def test_edit_packet_rejects_unknown_field(client):
    _posting, application = _posting_with_application()
    resp = client.put(f"/api/jobs/applications/{application.id}/packet", json={"cover_letter_text": "x", "extra": "nope"})
    assert resp.status_code == 422
