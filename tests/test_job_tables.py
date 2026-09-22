"""
Tests for the JobCompany / JobBoard / JobPosting tables in pipeline.db.

Coverage mirrors tests/test_domain_tables.py: create_all() on a temp engine, then exercise the
constraints that matter for correctness rather than every column. The two constraints that matter
here are UNIQUE(ats_kind, board_id) on JobBoard (a board is resolved at most once) and
UNIQUE(board_id, external_id) on JobPosting (a posting dedupes across overlapping polls).
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import JobBoard, JobCompany, JobPosting  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _company(session, name="Acme"):
    company = JobCompany(name=name, source="watchlist")
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


def test_create_all_makes_job_tables(temp_db):
    assert {"jobcompany", "jobboard", "jobposting"} <= set(SQLModel.metadata.tables)


def test_job_company_round_trip(temp_db):
    with Session(temp_db) as session:
        company = _company(session)
        assert company.id is not None
        assert company.status == "active"
        assert isinstance(company.first_seen, datetime) and isinstance(company.last_seen, datetime)

    with Session(temp_db) as session:
        stored = session.exec(select(JobCompany).where(JobCompany.name == "Acme")).one()
        assert (stored.source, stored.status) == ("watchlist", "active")


def test_job_board_round_trip(temp_db):
    with Session(temp_db) as session:
        company = _company(session)
        board = JobBoard(
            company_id=company.id,
            ats_kind="greenhouse",
            board_id="acme",
            resolved_by="slug_probe",
            confidence=0.9,
        )
        session.add(board)
        session.commit()
        session.refresh(board)
        assert board.id is not None
        assert board.verified_at is None
        assert board.last_polled_at is None
        assert board.last_error is None


def test_job_board_ats_kind_board_id_is_unique(temp_db):
    with Session(temp_db) as session:
        company = _company(session)
        session.add(
            JobBoard(company_id=company.id, ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0)
        )
        session.commit()

    with Session(temp_db) as session:
        other_company = _company(session, name="Other Co")
        session.add(
            JobBoard(
                company_id=other_company.id,
                ats_kind="greenhouse",
                board_id="acme",
                resolved_by="pattern",
                confidence=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_job_posting_round_trip(temp_db):
    with Session(temp_db) as session:
        company = _company(session)
        board = JobBoard(company_id=company.id, ats_kind="lever", board_id="acme", resolved_by="pattern", confidence=1.0)
        session.add(board)
        session.commit()
        session.refresh(board)

        posting = JobPosting(
            board_id=board.id,
            external_id="job-123",
            title="Backend Engineer",
            apply_url="https://jobs.lever.co/acme/job-123",
            content_hash="hash1",
        )
        session.add(posting)
        session.commit()
        session.refresh(posting)
        assert posting.id is not None
        assert posting.missed_polls == 0
        assert posting.closed_at is None
        assert posting.fit_score is None

    with Session(temp_db) as session:
        stored = session.exec(select(JobPosting).where(JobPosting.external_id == "job-123")).one()
        assert (stored.title, stored.content_hash) == ("Backend Engineer", "hash1")


def test_job_posting_board_id_external_id_is_unique(temp_db):
    with Session(temp_db) as session:
        company = _company(session)
        board = JobBoard(company_id=company.id, ats_kind="ashby", board_id="acme", resolved_by="pattern", confidence=1.0)
        session.add(board)
        session.commit()
        session.refresh(board)

        session.add(
            JobPosting(
                board_id=board.id,
                external_id="job-123",
                title="Backend Engineer",
                apply_url="https://jobs.ashbyhq.com/acme/job-123",
                content_hash="hash1",
            )
        )
        session.commit()

    with Session(temp_db) as session:
        board_id = session.exec(select(JobBoard.id)).one()
        session.add(
            JobPosting(
                board_id=board_id,
                external_id="job-123",
                title="Backend Engineer (dupe)",
                apply_url="https://jobs.ashbyhq.com/acme/job-123",
                content_hash="hash2",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
