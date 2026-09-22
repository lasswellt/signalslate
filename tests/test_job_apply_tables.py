"""
Tests for the JobsProfile / AnswerBank / JobApplication tables in pipeline.db.

Coverage mirrors tests/test_job_tables.py: create_all() on a temp engine, then exercise the
constraint that matters for correctness — UNIQUE(posting_id) on JobApplication (at most one
application per posting) — plus defaults and round trips for the other two tables.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import AnswerBank, JobApplication, JobBoard, JobCompany, JobPosting, JobsProfile  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _posting(session, external_id="job-123"):
    company = JobCompany(name="Acme", source="watchlist")
    session.add(company)
    session.commit()
    session.refresh(company)

    board = JobBoard(company_id=company.id, ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0)
    session.add(board)
    session.commit()
    session.refresh(board)

    posting = JobPosting(
        board_id=board.id,
        external_id=external_id,
        title="Backend Engineer",
        apply_url="https://jobs.greenhouse.io/acme/job-123",
        content_hash="hash1",
    )
    session.add(posting)
    session.commit()
    session.refresh(posting)
    return posting


def test_create_all_makes_apply_tables(temp_db):
    assert {"jobsprofile", "answerbank", "jobapplication"} <= set(SQLModel.metadata.tables)


def test_jobs_profile_defaults(temp_db):
    with Session(temp_db) as session:
        profile = JobsProfile()
        session.add(profile)
        session.commit()
        session.refresh(profile)
        assert profile.id is not None
        assert profile.other_links == "[]"
        assert profile.salary_disclosure_policy == "decline"
        assert profile.eeo_answers == "{}"
        assert profile.target_roles == "[]"
        assert profile.target_locations == "[]"
        assert profile.target_exclusions == "[]"
        assert profile.resume_paths == "[]"
        assert isinstance(profile.updated_at, datetime)
        assert profile.work_authorized is None
        assert profile.full_name is None


def test_answer_bank_round_trip_and_reuse(temp_db):
    with Session(temp_db) as session:
        posting = _posting(session)
        application = JobApplication(posting_id=posting.id)
        session.add(application)
        session.commit()
        session.refresh(application)

        # Two rows for the same normalized question are allowed — question_norm is not unique.
        session.add(
            AnswerBank(
                question_norm="why do you want to work here",
                question_raw="Why do you want to work here?",
                answer="Old answer.",
                source_application_id=application.id,
            )
        )
        session.add(
            AnswerBank(
                question_norm="why do you want to work here",
                question_raw="Why do you want to work here?",
                answer="Updated answer.",
                source_application_id=application.id,
            )
        )
        session.commit()

    with Session(temp_db) as session:
        rows = session.exec(
            select(AnswerBank).where(AnswerBank.question_norm == "why do you want to work here")
        ).all()
        assert len(rows) == 2
        assert {row.answer for row in rows} == {"Old answer.", "Updated answer."}


def test_job_application_round_trip(temp_db):
    with Session(temp_db) as session:
        posting = _posting(session)
        posting_id = posting.id
        application = JobApplication(posting_id=posting_id)
        session.add(application)
        session.commit()
        session.refresh(application)
        assert application.id is not None
        assert application.status == "draft"
        assert application.assist_state == "idle"
        assert application.packet is None
        assert application.assist_session_id is None
        assert application.submitted_at is None
        assert isinstance(application.created_at, datetime)

    with Session(temp_db) as session:
        stored = session.exec(select(JobApplication).where(JobApplication.posting_id == posting_id)).one()
        assert (stored.status, stored.assist_state) == ("draft", "idle")


def test_job_application_posting_id_is_unique(temp_db):
    with Session(temp_db) as session:
        posting = _posting(session)
        session.add(JobApplication(posting_id=posting.id))
        session.commit()

    with Session(temp_db) as session:
        posting_id = session.exec(select(JobPosting.id)).one()
        session.add(JobApplication(posting_id=posting_id))
        with pytest.raises(IntegrityError):
            session.commit()
