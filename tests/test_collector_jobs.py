"""
Tests for pipeline.collectors.jobs.collect_jobs(): no network (this collector never makes one —
see its module docstring), so these exercise the DB-read wiring directly against a temp SQLite
engine, following tests/test_collector_domains.py's temp_db fixture.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, health  # noqa: E402
from pipeline.collectors import CollectionResult, Item  # noqa: E402
from pipeline.collectors.jobs import collect_jobs  # noqa: E402
from pipeline.db import JobApplication, JobBoard, JobCompany, JobPosting  # noqa: E402

SINCE = datetime(2026, 9, 13, 6, 0, 0)
UNTIL = datetime(2026, 9, 14, 6, 0, 0)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    # JOBS_SCORE_THRESHOLD is read through pipeline.health.env(); point it at an empty .env in
    # tmp_path so every test gets the documented default (0.6) unless it writes its own.
    monkeypatch.setattr(health, "ROOT", tmp_path)
    return engine


def _add_company(session, name="Acme Inc", **kw):
    company = JobCompany(name=name, source="manual", **kw)
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


def _add_board(session, company, **kw):
    board = JobBoard(
        company_id=company.id,
        ats_kind=kw.pop("ats_kind", "greenhouse"),
        board_id=kw.pop("board_id", "acme"),
        resolved_by=kw.pop("resolved_by", "pattern"),
        confidence=kw.pop("confidence", 1.0),
        **kw,
    )
    session.add(board)
    session.commit()
    session.refresh(board)
    return board


def _add_posting(session, board, **kw):
    posting = JobPosting(
        board_id=board.id,
        external_id=kw.pop("external_id", "job-1"),
        title=kw.pop("title", "Staff Engineer"),
        apply_url=kw.pop("apply_url", "https://example.com/apply"),
        content_hash=kw.pop("content_hash", "hash"),
        first_seen=kw.pop("first_seen", SINCE + timedelta(hours=1)),
        **kw,
    )
    session.add(posting)
    session.commit()
    session.refresh(posting)
    return posting


def _add_application(session, posting, **kw):
    application = JobApplication(posting_id=posting.id, **kw)
    session.add(application)
    session.commit()
    session.refresh(application)
    return application


def test_no_data_returns_ok_empty(temp_db):
    result = collect_jobs(SINCE, UNTIL)
    assert isinstance(result, CollectionResult)
    assert result.source == "jobs"
    assert result.status == "ok"
    assert result.items == []


def test_new_posting_above_threshold_yields_job_new_item(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        posting = _add_posting(session, board, fit_score=75, fit_reason="strong match")
        posting_id = posting.id

    result = collect_jobs(SINCE, UNTIL)
    assert result.status == "ok"
    assert len(result.items) == 1
    item = result.items[0]
    assert isinstance(item, Item)
    assert item.item_type == "job_new"
    assert item.external_id == f"job_new:{posting_id}"
    assert item.occurred_at == SINCE + timedelta(hours=1)
    assert item.payload == {
        "posting_id": posting_id,
        "title": "Staff Engineer",
        "company": "Acme Inc",
        "fit_score": 75,
        "fit_reason": "strong match",
        "apply_url": "https://example.com/apply",
    }


def test_new_posting_below_threshold_is_excluded(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        _add_posting(session, board, fit_score=10, fit_reason="weak match")

    result = collect_jobs(SINCE, UNTIL)
    assert result.status == "ok"
    assert result.items == []


def test_unscored_posting_is_excluded(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        _add_posting(session, board, fit_score=None)

    result = collect_jobs(SINCE, UNTIL)
    assert result.items == []


def test_posting_first_seen_outside_window_is_excluded(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        _add_posting(session, board, fit_score=90, first_seen=SINCE - timedelta(hours=1))

    result = collect_jobs(SINCE, UNTIL)
    assert result.items == []


def test_closed_posting_with_application_yields_job_closed_item(temp_db):
    with db.get_session() as session:
        company = _add_company(session, name="Beta LLC")
        board = _add_board(session, company)
        posting = _add_posting(
            session, board, first_seen=SINCE - timedelta(days=2), closed_at=SINCE + timedelta(hours=2)
        )
        posting_id = posting.id
        _add_application(session, posting, status="in_progress")

    result = collect_jobs(SINCE, UNTIL)
    assert result.status == "ok"
    closed_items = [i for i in result.items if i.item_type == "job_closed"]
    assert len(closed_items) == 1
    item = closed_items[0]
    assert item.external_id == f"job_closed:{posting_id}:{(SINCE + timedelta(hours=2)).isoformat()}"
    assert item.occurred_at == SINCE + timedelta(hours=2)
    assert item.payload == {
        "posting_id": posting_id,
        "title": "Staff Engineer",
        "company": "Beta LLC",
        "application_status": "in_progress",
    }


def test_closed_posting_without_application_is_excluded(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        _add_posting(session, board, first_seen=SINCE - timedelta(days=2), closed_at=SINCE + timedelta(hours=2))

    result = collect_jobs(SINCE, UNTIL)
    assert not any(i.item_type == "job_closed" for i in result.items)


def test_closed_at_outside_window_is_excluded(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        posting = _add_posting(
            session, board, first_seen=SINCE - timedelta(days=2), closed_at=SINCE - timedelta(hours=1)
        )
        _add_application(session, posting, status="submitted")

    result = collect_jobs(SINCE, UNTIL)
    assert not any(i.item_type == "job_closed" for i in result.items)


def test_submitted_application_yields_application_update_item(temp_db):
    with db.get_session() as session:
        company = _add_company(session, name="Gamma Co")
        board = _add_board(session, company)
        posting = _add_posting(session, board, first_seen=SINCE - timedelta(days=5), title="Backend Engineer")
        posting_id = posting.id
        application = _add_application(
            session, posting, status="submitted", submitted_at=SINCE + timedelta(hours=3)
        )
        application_id = application.id

    result = collect_jobs(SINCE, UNTIL)
    assert result.status == "ok"
    update_items = [i for i in result.items if i.item_type == "application_update"]
    assert len(update_items) == 1
    item = update_items[0]
    assert item.external_id == f"application_update:{application_id}:submitted"
    assert item.occurred_at == SINCE + timedelta(hours=3)
    assert item.payload == {
        "posting_id": posting_id,
        "title": "Backend Engineer",
        "company": "Gamma Co",
        "status": "submitted",
    }


def test_submitted_at_outside_window_is_excluded(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        posting = _add_posting(session, board, first_seen=SINCE - timedelta(days=5))
        _add_application(session, posting, status="submitted", submitted_at=SINCE - timedelta(hours=1))

    result = collect_jobs(SINCE, UNTIL)
    assert not any(i.item_type == "application_update" for i in result.items)


def test_missing_posting_for_application_is_partial_not_raised(temp_db):
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        posting = _add_posting(session, board, first_seen=SINCE - timedelta(days=5))
        posting_id = posting.id
        _add_application(session, posting, status="submitted", submitted_at=SINCE + timedelta(hours=1))
        # Simulate a since-deleted posting: drop the row but leave the application pointing at it.
        session.delete(session.get(JobPosting, posting_id))
        session.commit()

    result = collect_jobs(SINCE, UNTIL)
    assert result.status == "partial"
    assert not any(i.item_type == "application_update" for i in result.items)
    assert str(posting_id) in result.detail


def test_custom_score_threshold_is_honored(temp_db, tmp_path):
    (tmp_path / ".env").write_text("JOBS_SCORE_THRESHOLD=0.9\n")
    with db.get_session() as session:
        company = _add_company(session)
        board = _add_board(session, company)
        _add_posting(session, board, fit_score=85, fit_reason="good but not great")

    result = collect_jobs(SINCE, UNTIL)
    assert result.items == []


def test_db_session_error_returns_error_status_not_raised(temp_db, monkeypatch):
    def boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(db, "get_session", boom)
    result = collect_jobs(SINCE, UNTIL)
    assert result.source == "jobs"
    assert result.status == "error"
    assert result.items == []
