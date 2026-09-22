"""
Tests for the management-UI additions to pipeline.runner.execute_run: single-source runs, the atomic
run guard, the per-run env snapshot and last-attempt recording, plus the config_store toggles they
lean on.

Unlike tests/test_runner.py these use the real config store and the real source list (declared in a
tmp .env), so `only` is validated against the same known_sources() a deployment would use. Only the
network edge is stubbed: the health check and the collector call.
"""
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config_store, db, health, runner  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors import CollectionResult, Item  # noqa: E402
from pipeline.health import HealthResult  # noqa: E402

SECRET = "xoxp-do-not-leak-0123456789"


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def declared(monkeypatch, tmp_path):
    """Two Slack workspaces declared in a tmp .env, and a tmp config.json."""
    monkeypatch.setattr(health, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("SLACK_ALPHA_TOKEN=xoxp-a\nSLACK_BETA_TOKEN=xoxp-b\n")
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "data" / "config.json")


def stub_network(monkeypatch, results=None):
    """
    Health check reports every requested source ok; the collector returns `results[source]` (or an
    empty ok result). Returns the list of (source) collected and the list of active-source dicts
    the health check was asked about.
    """
    collected: list[str] = []
    asked: list[dict] = []

    def check(active):
        asked.append(dict(active))
        return [HealthResult(s, "ok", "") for s, on in active.items() if on]

    def collect(source, _until):
        collected.append(source)
        if results is not None and source in results:
            outcome = results[source]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return CollectionResult(source, "ok", "fine")

    monkeypatch.setattr(runner, "check_all_configured", check)
    monkeypatch.setattr(runner, "_collect_source", collect)
    return collected, asked


def item(external_id: str) -> Item:
    return Item("message", external_id, utcnow(), {"text": "hi"})


def cursor_of(source: str) -> db.SourceCursor:
    row = db.get_cursor(source)
    assert row is not None
    return row


def run_rows() -> list:
    with db.get_session() as session:
        return list(session.exec(select(db.Run)).all())


# --- single-source runs -------------------------------------------------------------


def test_only_collects_just_that_source_and_leaves_others_cursors(temp_db, declared, monkeypatch):
    old = utcnow() - timedelta(hours=30)
    db.set_cursor("slack_alpha", old)
    db.set_cursor("slack_beta", old)
    collected, asked = stub_network(monkeypatch)

    run = runner.execute_run(only="slack_alpha")

    assert collected == ["slack_alpha"]
    assert asked == [{"slack_alpha": True}]
    assert run.trigger == "manual-source"
    assert run.status == "success"
    assert run.id is not None
    assert [h.source for h in db.source_health_for_run(run.id)] == ["slack_alpha"]
    advanced = cursor_of("slack_alpha").last_success_at
    assert advanced is not None and advanced > old
    assert cursor_of("slack_beta").last_success_at == old
    assert cursor_of("slack_beta").last_status is None


def test_only_runs_a_source_that_is_toggled_off(temp_db, declared, monkeypatch):
    config_store.set_source_active("slack_alpha", False)
    collected, _ = stub_network(monkeypatch)

    runner.execute_run(only="slack_alpha")

    assert collected == ["slack_alpha"]


def test_only_none_still_collects_every_active_source(temp_db, declared, monkeypatch):
    config_store.set_source_active("slack_beta", False)
    collected, _ = stub_network(monkeypatch)

    run = runner.execute_run()

    assert collected == ["zoom", "slack_alpha", "domains"]  # zoom/domains are always declared; beta is off
    assert run.trigger == "manual"


def test_only_keeps_the_failure_streak_logic(temp_db, declared, monkeypatch):
    stub_network(monkeypatch, {"slack_alpha": CollectionResult("slack_alpha", "error", "rate limited")})

    run = runner.execute_run(only="slack_alpha")

    assert run.status == "failed"
    cursor = cursor_of("slack_alpha")
    assert cursor.consecutive_failures == 1
    assert cursor.last_success_at is None


def test_unknown_only_raises_and_writes_no_run(temp_db, declared, monkeypatch):
    collected, asked = stub_network(monkeypatch)

    with pytest.raises(ValueError):
        runner.execute_run(only="slack_nope")

    assert run_rows() == []
    assert collected == [] and asked == []


# --- atomic run guard ---------------------------------------------------------------


def test_two_concurrent_runs_yield_one_run_row(temp_db, declared, monkeypatch):
    release = threading.Event()
    barrier = threading.Barrier(2)

    def check(active):
        assert release.wait(timeout=10)
        return [HealthResult(s, "ok", "") for s, on in active.items() if on]

    real_has_running = runner.has_running_run

    def slow_has_running():
        # The gap between the read and the insert is what the lock must cover; widen it so an
        # unguarded implementation loses this race every time instead of once in a thousand.
        answer = real_has_running()
        time.sleep(0.15)
        return answer

    monkeypatch.setattr(runner, "check_all_configured", check)
    monkeypatch.setattr(runner, "_collect_source", lambda source, _until: CollectionResult(source, "ok", ""))
    monkeypatch.setattr(runner, "has_running_run", slow_has_running)

    outcomes: list[object] = []

    def attempt():
        barrier.wait(timeout=10)
        try:
            outcomes.append(runner.execute_run())
        except runner.RunAlreadyInProgress as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()

    # The loser returns at once; the winner is parked in the health check until released.
    deadline = time.monotonic() + 10
    while len(outcomes) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert len(run_rows()) == 1
    assert sum(isinstance(o, runner.RunAlreadyInProgress) for o in outcomes) == 1
    assert sum(isinstance(o, db.Run) for o in outcomes) == 1


# --- per-run snapshot ---------------------------------------------------------------


def test_store_change_between_health_and_collect_is_not_observed(temp_db, declared, monkeypatch):
    class Overlay:
        keys = {"SLACK_GAMMA_TOKEN": "xoxp-old"}

        def __call__(self):
            return self.keys

    overlay = Overlay()
    health.set_env_overlay_provider(overlay, lambda key: key.startswith("SLACK_") and key.endswith("_TOKEN"))
    seen: dict = {}

    def check(active):
        # A connection edit landing mid-run: gamma's token changes and delta appears.
        overlay.keys = {"SLACK_GAMMA_TOKEN": "xoxp-new", "SLACK_DELTA_TOKEN": "xoxp-d"}
        return [HealthResult(s, "ok", "") for s, on in active.items() if on]

    def collect(source, _until):
        seen["sources"] = health.known_sources()
        seen["gamma"] = health.slack_workspaces().get("gamma")
        return CollectionResult(source, "ok", "")

    monkeypatch.setattr(runner, "check_all_configured", check)
    monkeypatch.setattr(runner, "_collect_source", collect)

    runner.execute_run(only="slack_gamma")

    assert seen["sources"] == ["zoom", "slack_gamma", "domains"]
    assert seen["gamma"] == "xoxp-old"
    # The change really happened; only the run was shielded from it.
    assert health.known_sources() == ["zoom", "slack_delta", "slack_gamma", "domains"]
    assert health.slack_workspaces()["gamma"] == "xoxp-new"


# --- last-attempt recording ---------------------------------------------------------


def test_attempt_recorded_for_ok(temp_db, declared, monkeypatch):
    stub_network(monkeypatch, {"slack_alpha": CollectionResult("slack_alpha", "ok", "all good", [item("1"), item("2")])})

    runner.execute_run(only="slack_alpha")

    cursor = cursor_of("slack_alpha")
    assert (cursor.last_status, cursor.last_detail, cursor.last_item_count) == ("ok", "all good", 2)
    assert isinstance(cursor.last_attempt_at, datetime)


def test_attempt_recorded_for_partial_without_moving_the_watermark(temp_db, declared, monkeypatch):
    old = utcnow() - timedelta(hours=30)
    db.set_cursor("slack_alpha", old)
    stub_network(
        monkeypatch, {"slack_alpha": CollectionResult("slack_alpha", "partial", "1 channel failed", [item("1")])}
    )

    runner.execute_run(only="slack_alpha")

    cursor = cursor_of("slack_alpha")
    assert (cursor.last_status, cursor.last_detail, cursor.last_item_count) == ("partial", "1 channel failed", 1)
    assert cursor.last_success_at == old
    assert cursor.consecutive_failures == 1


def test_attempt_recorded_for_error(temp_db, declared, monkeypatch):
    stub_network(monkeypatch, {"slack_alpha": CollectionResult("slack_alpha", "error", "token revoked")})

    runner.execute_run(only="slack_alpha")

    cursor = cursor_of("slack_alpha")
    assert (cursor.last_status, cursor.last_detail, cursor.last_item_count) == ("error", "token revoked", 0)


def test_attempt_recorded_for_a_raised_exception_by_class_name_only(temp_db, declared, monkeypatch):
    stub_network(monkeypatch, {"slack_alpha": RuntimeError(f"boom {SECRET}")})

    run = runner.execute_run(only="slack_alpha")

    cursor = cursor_of("slack_alpha")
    assert (cursor.last_status, cursor.last_detail, cursor.last_item_count) == ("error", "RuntimeError", None)
    assert cursor.consecutive_failures == 1
    assert run.status == "failed"
    assert SECRET not in (cursor.last_detail or "")


def test_unhealthy_source_records_no_attempt(temp_db, declared, monkeypatch):
    monkeypatch.setattr(runner, "check_all_configured", lambda active: [HealthResult("slack_alpha", "error", "no")])
    collected: list[str] = []
    monkeypatch.setattr(runner, "_collect_source", lambda s, _u: collected.append(s))

    runner.execute_run(only="slack_alpha")

    assert collected == []
    assert db.get_cursor("slack_alpha") is None


# --- config_store toggles -----------------------------------------------------------


def test_set_source_active_round_trip(declared):
    assert config_store.load_config()["active_sources"]["slack_alpha"] is True

    merged = config_store.set_source_active("slack_alpha", False)

    assert merged["active_sources"]["slack_alpha"] is False
    assert config_store.load_config()["active_sources"]["slack_alpha"] is False
    assert config_store.load_config()["active_sources"]["slack_beta"] is True

    config_store.set_source_active("slack_alpha", True)
    assert config_store.load_config()["active_sources"]["slack_alpha"] is True


def test_set_source_active_ignores_an_undeclared_id(declared):
    merged = config_store.set_source_active("slack_nope", False)

    assert "slack_nope" not in merged["active_sources"]
    assert "slack_nope" not in config_store.load_config()["active_sources"]


def test_forgotten_source_does_not_resurrect_its_old_toggle(declared, tmp_path):
    config_store.set_source_active("slack_alpha", False)
    config_store.set_source_active("slack_beta", False)

    config_store.forget_source("slack_alpha")

    stored = (tmp_path / "data" / "config.json").read_text()
    assert "slack_alpha" not in stored
    cfg = config_store.load_config()
    assert cfg["active_sources"]["slack_alpha"] is True  # re-created source starts on
    assert cfg["active_sources"]["slack_beta"] is False  # neighbours untouched


def test_forget_source_survives_a_missing_file_and_an_unknown_id(declared):
    config_store.forget_source("slack_alpha")  # no config.json yet
    config_store.load_config()
    config_store.forget_source("slack_never_existed")


def test_forget_source_never_calls_the_connection_overlay(declared):
    config_store.set_source_active("slack_alpha", False)
    calls: list[int] = []

    def provider():
        calls.append(1)
        return {}

    health.set_env_overlay_provider(provider, lambda key: False)
    calls.clear()

    config_store.forget_source("slack_alpha")

    assert calls == []
