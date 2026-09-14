"""
Tests for pipeline.runner.execute_run against a throwaway SQLite file.

The engine in pipeline.db is created at import time, so these tests rebuild it against tmp_path and
re-point the module's globals — same monkeypatch-the-module-constants style as tests/test_health.py.
No network: check_all_configured is stubbed at the module boundary.
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, runner  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
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
    """
    Active sources, plus a collector stub.

    _collect_source is stubbed by default so orchestration tests can never reach the network —
    a test that wants collection behaviour opts in with stub_collect().
    """
    monkeypatch.setattr(runner, "load_config", lambda: {"active_sources": {"zoom": True}})
    monkeypatch.setattr(runner, "_active", lambda _config: [])


def stub_collect(monkeypatch, results: dict):
    """Make each source id collect a canned CollectionResult, and declare those sources active."""
    monkeypatch.setattr(runner, "_active", lambda _config: list(results))
    monkeypatch.setattr(runner, "_collect_source", lambda source, _until: results[source])


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
                        occurred_at=utcnow(),
                        payload="{}",
                    )
                )
        session.commit()

    assert db.item_counts_for_run(1) == {"zoom": 2, "slack_a": 3}


def test_summary_reports_collected_count(temp_db, no_config, monkeypatch):
    from pipeline.collectors import CollectionResult, Item

    items = [Item("meeting", f"m{i}", utcnow(), {"n": i}) for i in range(7)]
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "ok", "7 meetings", items)})

    run = runner.execute_run()
    assert run.status == "success"
    assert "Collected 7 items" in run.summary
    assert "zoom=7" in run.summary


def test_summary_says_nothing_new_when_empty(temp_db, no_config, monkeypatch):
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    run = runner.execute_run()
    assert "nothing new" in run.summary


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


# --- schedule timezone ---------------------------------------------------------------


def test_schedule_timezone_reads_env(monkeypatch):
    from api import scheduler

    monkeypatch.setenv("TZ", "America/New_York")
    assert str(scheduler.schedule_timezone()) == "America/New_York"


def test_schedule_timezone_none_when_unset(monkeypatch, tmp_path):
    from api import scheduler

    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(scheduler, "ROOT", tmp_path)  # .env absent
    assert scheduler.schedule_timezone() is None


def test_bad_timezone_falls_back_instead_of_crashing(monkeypatch):
    from api import scheduler

    monkeypatch.setenv("TZ", "Not/AZone")
    assert scheduler.schedule_timezone() is None


def test_cron_fires_in_configured_timezone(monkeypatch):
    """0 6 * * * in New York is 10:00 or 11:00 UTC, never 06:00 UTC."""
    from datetime import datetime, timezone as dt_timezone

    from apscheduler.triggers.cron import CronTrigger

    from api import scheduler

    monkeypatch.setenv("TZ", "America/New_York")
    trigger = CronTrigger.from_crontab("0 6 * * *", timezone=scheduler.schedule_timezone())
    after = datetime(2026, 9, 13, 0, 0, tzinfo=dt_timezone.utc)
    fire = trigger.get_next_fire_time(None, after)
    assert fire.astimezone(dt_timezone.utc).hour == 10  # EDT, UTC-4


def test_scheduler_timezone_is_explicit_not_guessed():
    """
    APScheduler left to itself asks tzlocal to guess from the system clock, which warns and picks
    arbitrarily when a container's /etc/localtime disagrees with its actual offset.
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import importlib

        from api import scheduler

        importlib.reload(scheduler)

    assert scheduler._scheduler.timezone is not None
    assert not [w for w in caught if "Timezone offset does not match" in str(w.message)]


# --- orphan reaping + partial semantics (from the code review) ------------------------


def test_reap_marks_stranded_runs_failed(temp_db):
    """A container restart mid-run would otherwise block every future run forever."""
    with db.get_session() as session:
        session.add(db.Run(trigger="scheduled", status="running"))
        session.add(db.Run(trigger="manual", status="success"))
        session.commit()

    assert db.reap_orphaned_runs() == 1
    assert db.has_running_run() is False

    with db.get_session() as session:
        reaped = session.exec(select(db.Run).where(db.Run.trigger == "scheduled")).first()
    assert reaped.status == "failed"
    assert "Interrupted" in reaped.error
    assert reaped.finished_at is not None


def test_reap_is_a_noop_when_nothing_stranded(temp_db):
    assert db.reap_orphaned_runs() == 0


def test_reap_unblocks_a_deadlocked_trigger(temp_db, no_config, monkeypatch):
    with db.get_session() as session:
        session.add(db.Run(trigger="manual", status="running"))
        session.commit()

    with pytest.raises(runner.RunAlreadyInProgress):
        runner.execute_run()

    db.reap_orphaned_runs()
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    assert runner.execute_run().status == "success"


def test_partial_collection_does_not_advance_the_cursor(temp_db, no_config, monkeypatch):
    """A half-failed catch-up run must re-read its window, not skip the rest of the gap."""
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "partial", "3 of 8 channels failed")})

    run = runner.execute_run()
    # A cursor row now exists to hold the failure streak, but the watermark itself never moved.
    assert db.get_cursor("zoom").last_success_at is None
    assert db.get_cursor("zoom").consecutive_failures == 1
    assert run.status == "partial"


def test_partial_collection_is_reported_not_called_healthy(temp_db, no_config, monkeypatch):
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "partial", "chat collector 500'd")})

    run = runner.execute_run()
    assert "healthy" not in (run.summary or "")
    assert "chat collector" in (run.error or "")


def test_clean_collection_does_advance_the_cursor(temp_db, no_config, monkeypatch):
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "ok", "all good")})

    runner.execute_run()
    assert db.get_cursor("zoom").last_success_at is not None


# --- window bounds + dedupe ----------------------------------------------------------


def test_window_is_24h_with_no_cursor(temp_db):
    until = datetime(2026, 9, 14, 6, 0, 0)
    assert runner.collection_window("zoom", until) == until - runner.DEFAULT_LOOKBACK


def test_window_backfills_from_a_lagging_cursor(temp_db):
    until = datetime(2026, 9, 14, 6, 0, 0)
    db.set_cursor("zoom", datetime(2026, 9, 11, 6, 0, 0))
    # Three days back, minus the overlap — not the default 24h.
    assert runner.collection_window("zoom", until) == datetime(2026, 9, 11, 5, 30, 0)


def test_window_is_capped_for_a_very_stale_cursor(temp_db):
    """A machine back after a month must not ask every API for a month of history."""
    until = datetime(2026, 9, 14, 6, 0, 0)
    db.set_cursor("zoom", datetime(2026, 1, 1, 0, 0, 0))

    window = runner.collection_window("zoom", until)
    assert window == until - runner.MAX_BACKFILL
    assert (until - window).days == 7


def test_window_never_shrinks_below_the_default(temp_db):
    """A cursor from five minutes ago must not shorten the window to five minutes."""
    until = datetime(2026, 9, 14, 6, 0, 0)
    db.set_cursor("zoom", until - timedelta(minutes=5))
    assert runner.collection_window("zoom", until) == until - runner.DEFAULT_LOOKBACK


def test_persist_dedupes_within_one_batch(temp_db):
    """Duplicates inside a single result — a shifting page boundary — must not double-insert."""
    from pipeline.collectors import CollectionResult, Item

    with db.get_session() as session:
        session.add(db.Run(trigger="manual", status="running"))
        session.commit()

    dupe = Item("message", "same-id", utcnow(), {"text": "once"})
    result = CollectionResult("slack_a", "ok", "", [dupe, dupe, Item("message", "other", utcnow(), {})])

    assert runner._persist(1, result) == 2
    assert len(db.items_for_run(1)) == 2


def test_persist_dedupes_against_earlier_runs(temp_db):
    from pipeline.collectors import CollectionResult, Item

    with db.get_session() as session:
        session.add(db.Run(trigger="manual", status="running"))
        session.commit()

    first = CollectionResult("slack_a", "ok", "", [Item("message", "m1", utcnow(), {})])
    assert runner._persist(1, first) == 1
    assert runner._persist(1, first) == 0  # same window re-read, nothing new


def test_items_kept_when_a_source_errors_midway(temp_db, no_config, monkeypatch):
    """Half of Slack before the rate-limit canary is better than none of it."""
    from pipeline.collectors import CollectionResult, Item

    partial_haul = [Item("message", "got-this-one", utcnow(), {"text": "kept"})]
    stub_checks(monkeypatch, [HealthResult("slack_a", "ok", "")])
    stub_collect(monkeypatch, {"slack_a": CollectionResult("slack_a", "error", "canary fired", partial_haul)})

    run = runner.execute_run()
    assert run.status == "failed"           # sole source failed to collect
    assert len(db.items_for_run(run.id)) == 1  # and the partial haul is kept
    assert db.get_cursor("slack_a").last_success_at is None  # but the window will be re-read


def test_all_sources_failing_collection_is_failed_not_partial(temp_db, no_config, monkeypatch):
    """Health passing means nothing if every collection failed — that isn't a minor problem."""
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", ""), HealthResult("slack_a", "ok", "")])
    stub_collect(monkeypatch, {
        "zoom": CollectionResult("zoom", "error", "dead"),
        "slack_a": CollectionResult("slack_a", "error", "also dead"),
    })

    run = runner.execute_run()
    assert run.status == "failed"


def test_lone_partial_source_is_partial_not_failed(temp_db, no_config, monkeypatch):
    """A source that collected some of its window is not a hard failure."""
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "partial", "1 of 3 lists failed")})

    run = runner.execute_run()
    assert run.status == "partial"
    assert "1 partial" in run.summary


def test_healthy_source_with_failed_collection_is_not_counted_ok(temp_db, no_config, monkeypatch):
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", ""), HealthResult("slack_a", "ok", "")])
    stub_collect(monkeypatch, {
        "zoom": CollectionResult("zoom", "ok", "fine"),
        "slack_a": CollectionResult("slack_a", "error", "canary fired"),
    })

    run = runner.execute_run()
    assert run.status == "partial"
    assert "1 ok, 1 failing" in run.summary


def test_status_tiers_stay_disjoint_when_a_source_is_both_unhealthy_and_partial(temp_db):
    """Not reachable today (collection is skipped for unhealthy sources) — pinned so it stays safe."""
    status, summary, error = runner._summarize(
        health=[HealthResult("zoom", "error", "dead token"), HealthResult("slack_a", "ok", "")],
        collected={"slack_a": 2},
        failures=["zoom: dead token"],
        failed_sources={"zoom"},
        partial_sources={"zoom"},   # contradictory input: must not be counted as good
    )
    assert status == "partial"
    assert "1 ok" in summary
    assert "2 ok" not in summary


def test_watermark_advances_after_a_persistent_failure_streak(temp_db, no_config, monkeypatch):
    """
    A permanently-failing sub-resource must not freeze the watermark forever.

    Holding it back assumes failures are transient. A tenant without To Do licensed 403s every run,
    which would pin the window at MAX_BACKFILL and re-fetch a week of Graph daily, for good.
    """
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "partial", "todo 403s every time")})

    for expected_streak in range(1, runner.MAX_STUCK_RUNS):
        runner.execute_run()
        assert db.get_cursor("zoom").last_success_at is None
        assert db.get_cursor("zoom").consecutive_failures == expected_streak

    run = runner.execute_run()  # the MAX_STUCK_RUNS-th failure
    assert db.get_cursor("zoom").last_success_at is not None  # unfrozen
    assert "advancing watermark" in (run.error or "")


def test_a_clean_run_resets_the_failure_streak(temp_db, no_config, monkeypatch):
    from pipeline.collectors import CollectionResult

    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "partial", "flaky")})
    runner.execute_run()
    assert db.get_cursor("zoom").consecutive_failures == 1

    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "ok", "recovered")})
    runner.execute_run()
    assert db.get_cursor("zoom").consecutive_failures == 0


def test_capped_backfill_is_reported_not_silently_skipped(temp_db, no_config, monkeypatch):
    """Advancing past an unreachable month without saying so would hide a hole in the data."""
    from pipeline.collectors import CollectionResult

    db.set_cursor("zoom", utcnow() - timedelta(days=30))
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "ok", "fine")})

    run = runner.execute_run()
    # A caveat on a successful run, not a failure — but it must be visible on the dashboard.
    assert run.status == "success"
    assert "backfill capped" in run.summary
    assert "before the window were skipped" in run.summary


def test_no_shortfall_reported_for_a_normal_run(temp_db, no_config, monkeypatch):
    from pipeline.collectors import CollectionResult

    db.set_cursor("zoom", utcnow() - timedelta(hours=26))
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "ok", "fine")})

    run = runner.execute_run()
    assert run.status == "success"
    assert "backfill capped" not in run.summary  # must not fire spuriously


def test_sub_day_shortfall_is_still_reported(temp_db, no_config, monkeypatch):
    """A 20-hour hole truncates to 0 whole days — and a falsy 0 is silence."""
    from pipeline.collectors import CollectionResult

    db.set_cursor("zoom", utcnow() - (runner.MAX_BACKFILL + timedelta(hours=20)))
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    stub_collect(monkeypatch, {"zoom": CollectionResult("zoom", "ok", "fine")})

    run = runner.execute_run()
    assert "backfill capped" in run.summary
    # ~20h plus the OVERLAP the window genuinely reaches back for; the point is it isn't silent,
    # and specifically isn't the "0h" that whole-day truncation would have produced.
    hours = int(re.search(r"capped — (\d+)h", run.summary).group(1))
    assert 20 <= hours <= 21


def test_raising_collector_also_counts_toward_the_streak(temp_db, no_config, monkeypatch):
    """A collector that raises every run is the strongest case for eventually advancing."""
    stub_checks(monkeypatch, [HealthResult("zoom", "ok", "")])
    monkeypatch.setattr(runner, "_active", lambda _c: ["zoom"])

    def always_raises(source, until):
        raise ValueError("corrupt token cache")

    monkeypatch.setattr(runner, "_collect_source", always_raises)

    for _ in range(runner.MAX_STUCK_RUNS - 1):
        runner.execute_run()
        assert db.get_cursor("zoom").last_success_at is None

    run = runner.execute_run()
    assert db.get_cursor("zoom").last_success_at is not None
    assert "repeated hard failures" in (run.error or "")
