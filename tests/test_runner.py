"""
Tests for pipeline.runner.execute_run against a throwaway SQLite file.

The engine in pipeline.db is created at import time, so these tests rebuild it against tmp_path and
re-point the module's globals — same monkeypatch-the-module-constants style as tests/test_health.py.
No network: check_all_configured is stubbed at the module boundary.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, runner  # noqa: E402
from pipeline.health import HealthResult  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Point pipeline.db at a fresh file DB and create the schema."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def no_config(monkeypatch):
    """execute_run only uses config for active_sources; the stubbed checker ignores it."""
    monkeypatch.setattr(runner, "load_config", lambda: {"active_sources": {"zoom": True}})


def stub_checks(monkeypatch, results):
    monkeypatch.setattr(runner, "check_all_configured", lambda _active: results)


def stub_checks_raising(monkeypatch, exc):
    def boom(_active):
        raise exc

    monkeypatch.setattr(runner, "check_all_configured", boom)


# --- terminal status ---------------------------------------------------------------


def test_all_ok_is_success(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "fine")])
    run = runner.execute_run()
    assert run.status == "success"
    assert run.finished_at is not None
    assert run.error is None


def test_mixed_is_partial(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", ""), HealthResult("slack_a", "error", "nope")])
    run = runner.execute_run()
    assert run.status == "partial"
    assert "1 ok, 1 failing" in run.summary


def test_all_error_is_failed(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "error", "nope")])
    run = runner.execute_run()
    assert run.status == "failed"
    assert run.error


def test_no_sources_is_success(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [])
    run = runner.execute_run()
    assert run.status == "success"
    assert "No sources active" in run.summary


def test_source_health_rows_written(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "detail here")])
    run = runner.execute_run()
    rows = db.source_health_for_run(run.id)
    assert [(r.source, r.status, r.detail) for r in rows] == [("zoom", "ok", "detail here")]


# --- error isolation: the run must always reach a terminal status -------------------


def test_unexpected_exception_still_terminates_the_run(temp_db, no_config, monkeypatch):
    """The regression this guards: a raised collector left the Run stuck at "running" forever."""
    stub_checks_raising(monkeypatch, RuntimeError("corrupt token cache"))
    run = runner.execute_run()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "RuntimeError: corrupt token cache" in run.error


def test_no_run_is_left_running_after_failure(temp_db, no_config, monkeypatch):
    stub_checks_raising(monkeypatch, ValueError("boom"))
    runner.execute_run()
    with db.get_session() as session:
        stuck = session.exec(select(db.Run).where(db.Run.status == "running")).all()
    assert stuck == []


# --- overlap guard ------------------------------------------------------------------


def test_second_run_refused_while_one_is_running(temp_db, no_config):
    with db.get_session() as session:
        session.add(db.Run(trigger="scheduled", status="running"))
        session.commit()

    with pytest.raises(runner.RunAlreadyInProgress):
        runner.execute_run()


def test_run_allowed_once_previous_finished(temp_db, no_config, monkeypatch):
    with db.get_session() as session:
        session.add(db.Run(trigger="scheduled", status="success"))
        session.commit()

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    assert runner.execute_run().status == "success"


def test_has_running_run_reflects_state(temp_db):
    assert db.has_running_run() is False
    with db.get_session() as session:
        session.add(db.Run(trigger="manual", status="running"))
        session.commit()
    assert db.has_running_run() is True


# --- collected items + cursors ------------------------------------------------------


def test_item_counts_group_by_source(temp_db):
    with db.get_session() as session:
        session.add(db.Run(trigger="manual", status="running"))
        session.commit()
        for source, n in (("zoom", 2), ("slack_a", 3)):
            for i in range(n):
                session.add(
                    db.CollectedItem(
                        run_id=1,
                        source=source,
                        item_type="message",
                        external_id=f"{source}-{i}",
                        occurred_at=datetime.utcnow(),
                        payload="{}",
                    )
                )
        session.commit()

    assert db.item_counts_for_run(1) == {"zoom": 2, "slack_a": 3}


def test_summary_reports_collected_count(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    monkeypatch.setattr(runner, "item_counts_for_run", lambda _run_id: {"zoom": 7})
    run = runner.execute_run()
    assert "Collected 7 items" in run.summary


def test_summary_omits_count_when_nothing_collected(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    run = runner.execute_run()
    assert "Collected" not in run.summary


def test_set_cursor_inserts_then_updates(temp_db):
    first = datetime(2026, 9, 13, 6, 0, 0)
    second = datetime(2026, 9, 14, 6, 0, 0)

    db.set_cursor("zoom", first)
    assert db.get_cursor("zoom").last_success_at == first

    db.set_cursor("zoom", second, cursor="abc")
    row = db.get_cursor("zoom")
    assert row.last_success_at == second
    assert row.cursor == "abc"


def test_get_cursor_none_for_unknown_source(temp_db):
    assert db.get_cursor("never-collected") is None


def test_cursor_survives_a_failed_run(temp_db, no_config, monkeypatch):
    """The reason cursors aren't keyed to run_id: a failed run must not lose the watermark."""
    watermark = datetime(2026, 9, 13, 6, 0, 0)
    db.set_cursor("zoom", watermark)

    stub_checks_raising(monkeypatch, RuntimeError("collector exploded"))
    runner.execute_run()

    assert db.get_cursor("zoom").last_success_at == watermark
