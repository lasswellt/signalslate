"""
Tests for api.routers.collectors. Router-only app with install_security (the real guard and the
non-echoing 422 handler), never entered as a context manager so the real lifespan does not run.

The sources are real: two Slack connections created through pipeline.connections with the env overlay
registered the way startup registers it, so known_sources() is ["zoom", "slack_team", "slack_work"].
Only the edges are stubbed: runner.execute_run (the run route), collect.dry_run (job lifecycle) and
collect.dispatch (the network call under the real dry_run, for the end-to-end serialization test).
"""
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Union

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import collectors as collectors_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import collect, config_store, connections, crypto, db, health, runner  # noqa: E402
from pipeline.collectors import CollectionResult, Item  # noqa: E402
from pipeline.redact import MARKER  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
NOW = datetime(2026, 9, 21, 10, 5, 30)
WORK_TOKEN = "slack-token-Hd91XvB7mR"
TEAM_TOKEN = "slack-token-Lp48ZcN3wQ"
SHAPED = "xoxp-do-not-leak-0123456789"
Z_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def env(monkeypatch, tmp_path, temp_db):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)
    connections.create("slack", {"label": "work", "token": WORK_TOKEN})
    connections.create("slack", {"label": "team", "token": TEAM_TOKEN})
    yield tmp_path
    connections.set_vault(None)


@pytest.fixture
def client(env) -> TestClient:
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(collectors_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


@pytest.fixture(autouse=True)
def empty_job_table():
    collectors_router._jobs.clear()
    yield
    collectors_router._jobs.clear()


@pytest.fixture
def gate():
    """Holds stubbed dry runs open; released at teardown so no worker thread outlives its test."""
    event = threading.Event()
    yield event
    event.set()


@pytest.fixture
def clock(monkeypatch) -> dict[str, datetime]:
    now = {"value": NOW}
    monkeypatch.setattr(collectors_router, "utcnow", lambda: now["value"])
    return now


def dry_data(source: str) -> dict[str, Any]:
    """The shape pipeline.collect.dry_run returns, naive UTC datetimes included."""
    return {
        "source": source,
        "status": "ok",
        "detail": "fine",
        "window": {"since": NOW - timedelta(hours=24), "until": NOW, "hours": 24},
        "count": 1,
        "by_type": {"message": 1},
        "items": [{"item_type": "message", "occurred_at": NOW - timedelta(hours=1), "external_id": "m1", "preview": "hi"}],
        "duration_ms": 3,
    }


def stub_dry_run(monkeypatch, gate: Optional[threading.Event] = None, outcome: Union[dict, Exception, None] = None):
    """Replaces collect.dry_run. Returns the list of (source, hours, limit, raw) it was called with."""
    calls: list[tuple[str, int, int, bool]] = []

    def fake(source, hours=24, limit=5, *, raw=False):
        calls.append((source, hours, limit, raw))
        if gate is not None:
            assert gate.wait(10)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else dry_data(source)

    monkeypatch.setattr(collect, "dry_run", fake)
    return calls


def stub_execute_run(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runner, "execute_run", lambda **kwargs: calls.append(kwargs))
    return calls


def poll(client: TestClient, job_id: str, until: Callable[[dict], bool] = lambda body: body["status"] != "running") -> dict:
    deadline = time.monotonic() + 5
    while True:
        response = client.get(f"/api/collectors/dry-run/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if until(body) or time.monotonic() > deadline:
            return body
        time.sleep(0.01)


def start_dry_run(client: TestClient, source: str = "slack_work", **body: Any) -> str:
    response = client.post(f"/api/collectors/{source}/dry-run", json=body)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def add_items(source: str, *specs: tuple[str, str, Any]) -> list[int]:
    """(item_type, external_id, payload as dict or raw text) -> stored ids, in insertion order."""
    ids: list[int] = []
    with db.get_session() as session:
        run = db.Run(trigger="manual", status="success")
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        for item_type, external_id, payload in specs:
            row = db.CollectedItem(
                run_id=run.id,
                source=source,
                item_type=item_type,
                external_id=external_id,
                occurred_at=NOW - timedelta(minutes=5),
                payload=payload if isinstance(payload, str) else json.dumps(payload),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            assert row.id is not None
            ids.append(row.id)
    return ids


def cursor_of(source: str) -> db.SourceCursor:
    row = db.get_cursor(source)
    assert row is not None
    return row


def code_of(response) -> str:
    return response.json()["detail"]["code"]


# --- GET /collectors --------------------------------------------------------------------------


def test_state_lists_every_known_source_with_defaults(client):
    """A declared source is on until a toggle says otherwise (config_store)."""
    body = client.get("/api/collectors").json()

    assert [entry["source"] for entry in body] == health.known_sources() == ["zoom", "slack_team", "slack_work"]
    for entry in body:
        assert entry == {
            "source": entry["source"],
            "active": True,
            "watermark": None,
            "consecutive_failures": 0,
            "stuck_threshold": runner.MAX_STUCK_RUNS,
            "last_attempt": None,
            "item_count": 0,
        }


def test_state_reports_toggle_watermark_streak_last_attempt_and_item_count(client):
    config_store.set_source_active("slack_team", False)
    db.set_cursor("slack_work", NOW - timedelta(hours=2))
    db.record_failure("slack_work")
    db.record_failure("slack_work")
    db.record_attempt("slack_work", "error", "rate limited", 4, at=NOW - timedelta(minutes=10))
    add_items("slack_work", ("message", "a", {"text": "one"}), ("message", "b", {"text": "two"}))
    add_items("slack_team", ("message", "c", {"text": "other source"}))

    by_source = {entry["source"]: entry for entry in client.get("/api/collectors").json()}

    assert by_source["slack_work"] == {
        "source": "slack_work",
        "active": True,
        "watermark": "2026-09-21T08:05:30Z",
        "consecutive_failures": 2,
        "stuck_threshold": runner.MAX_STUCK_RUNS,
        "last_attempt": {"at": "2026-09-21T09:55:30Z", "status": "error", "detail": "rate limited", "item_count": 4},
        "item_count": 2,
    }
    assert by_source["slack_team"]["item_count"] == 1
    assert by_source["slack_team"]["active"] is False and by_source["zoom"]["active"] is True


def test_state_redacts_a_last_attempt_detail_carrying_a_secret(client):
    db.record_attempt("slack_work", "error", f"401 for {WORK_TOKEN} and {SHAPED} from {TEAM_TOKEN}", 0, at=NOW)

    response = client.get("/api/collectors")

    detail = next(e for e in response.json() if e["source"] == "slack_work")["last_attempt"]["detail"]
    assert MARKER in detail and detail.startswith("401 for ")
    for secret in (WORK_TOKEN, TEAM_TOKEN, SHAPED):
        assert secret not in response.text


# --- POST /collectors/{source}/run ------------------------------------------------------------


def test_run_accepts_and_starts_a_single_source_run(client, monkeypatch):
    calls = stub_execute_run(monkeypatch)
    config_store.set_source_active("slack_work", True)

    response = client.post("/api/collectors/slack_work/run")

    assert response.status_code == 202 and response.json() == {"accepted": True}
    assert calls == [{"trigger": "manual", "only": "slack_work"}]


def test_run_allows_a_source_that_is_toggled_off(client, monkeypatch):
    calls = stub_execute_run(monkeypatch)
    config_store.set_source_active("slack_work", False)

    response = client.post("/api/collectors/slack_work/run")

    assert response.status_code == 202
    assert calls == [{"trigger": "manual", "only": "slack_work"}]


def test_run_conflicts_while_a_run_is_in_flight(client, monkeypatch):
    calls = stub_execute_run(monkeypatch)
    with db.get_session() as session:
        session.add(db.Run(trigger="scheduled", status="running"))
        session.commit()

    response = client.post("/api/collectors/slack_work/run")

    assert response.status_code == 409 and code_of(response) == "run_in_progress"
    assert calls == []


def test_run_of_an_unknown_source_is_404_and_nothing_starts(client, monkeypatch):
    calls = stub_execute_run(monkeypatch)

    response = client.post("/api/collectors/slack_nope/run")

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "unknown_source", "message": "Unknown source"}}
    assert calls == []


# --- dry run ----------------------------------------------------------------------------------


def test_dry_run_job_lifecycle(client, monkeypatch, gate):
    calls = stub_dry_run(monkeypatch, gate)

    job_id = start_dry_run(client, hours=48, limit=2)
    running = client.get(f"/api/collectors/dry-run/{job_id}")

    assert running.status_code == 200 and running.json() == {"status": "running", "result": None}

    gate.set()
    done = poll(client, job_id)

    assert done["status"] == "done"
    assert done["result"] == {
        "source": "slack_work",
        "status": "ok",
        "detail": "fine",
        "window": {"since": "2026-09-20T10:05:30Z", "until": "2026-09-21T10:05:30Z", "hours": 24},
        "count": 1,
        "by_type": {"message": 1},
        "items": [{"item_type": "message", "occurred_at": "2026-09-21T09:05:30Z", "external_id": "m1", "preview": "hi"}],
        "duration_ms": 3,
    }
    assert calls == [("slack_work", 48, 2, False)]


def test_dry_run_defaults_are_24_hours_and_5_items(client, monkeypatch):
    calls = stub_dry_run(monkeypatch)

    poll(client, start_dry_run(client))

    assert calls == [("slack_work", 24, 5, False)]


def test_dry_run_rejects_out_of_range_arguments_before_starting(client, monkeypatch):
    calls = stub_dry_run(monkeypatch)

    for body in ({"hours": 0}, {"hours": 169}, {"limit": -1}, {"limit": 51}, {"hours": "soon"}):
        assert client.post("/api/collectors/slack_work/dry-run", json=body).status_code == 422
    assert calls == []
    assert collectors_router._jobs == {}


def test_dry_run_accepts_the_range_limits(client, monkeypatch):
    calls = stub_dry_run(monkeypatch)

    poll(client, start_dry_run(client, hours=168, limit=50))
    poll(client, start_dry_run(client, hours=1, limit=0))

    assert sorted(calls) == [("slack_work", 1, 0, False), ("slack_work", 168, 50, False)]


def test_dry_run_of_an_unknown_source_is_404(client, monkeypatch):
    calls = stub_dry_run(monkeypatch)

    response = client.post("/api/collectors/gmail_nope/dry-run", json={})

    assert response.status_code == 404 and code_of(response) == "unknown_source"
    assert calls == [] and collectors_router._jobs == {}


def test_unknown_job_is_404(client):
    response = client.get("/api/collectors/dry-run/does-not-exist")

    assert response.status_code == 404 and code_of(response) == "dry_run_not_found"


def test_dry_run_result_is_redacted_before_it_is_stored_or_returned(client, monkeypatch):
    dirty = dry_data("slack_work")
    dirty["detail"] = f"denied for {WORK_TOKEN}"
    dirty["items"][0]["preview"] = f"token {SHAPED} and {TEAM_TOKEN}"
    stub_dry_run(monkeypatch, outcome=dirty)

    job_id = start_dry_run(client)
    response = client.get(f"/api/collectors/dry-run/{job_id}")
    body = poll(client, job_id)

    assert body["status"] == "done"
    assert body["result"]["detail"] == f"denied for {MARKER}"
    assert MARKER in body["result"]["items"][0]["preview"]
    stored = repr(collectors_router._jobs[job_id].result)
    for secret in (WORK_TOKEN, TEAM_TOKEN, SHAPED):
        assert secret not in response.text and secret not in stored and secret not in json.dumps(body)


def test_a_dry_run_that_raises_fails_the_job_without_leaking_the_message(client, monkeypatch):
    stub_dry_run(monkeypatch, outcome=RuntimeError(f"upstream said {WORK_TOKEN}"))

    job_id = start_dry_run(client)
    response = client.get(f"/api/collectors/dry-run/{job_id}")
    body = poll(client, job_id)

    assert body == {"status": "failed", "result": None}
    assert WORK_TOKEN not in response.text and WORK_TOKEN not in json.dumps(body)


def test_real_dry_run_result_is_json_serializable_end_to_end(client, monkeypatch):
    """No stub on dry_run: its naive datetimes must reach the response as Z strings."""
    occurred = datetime(2026, 9, 21, 9, 15, 59)
    items = [
        Item("message", "m1", occurred, {"text": f"hello {SHAPED}", "when": occurred}),
        Item("message", "m2", occurred, {"text": "second"}),
    ]
    monkeypatch.setattr(collect, "dispatch", lambda source, since, until: CollectionResult(source, "ok", "2 things", items))

    job_id = start_dry_run(client, hours=6, limit=1)
    body = poll(client, job_id)

    assert body["status"] == "done"
    result = body["result"]
    json.dumps(result)
    assert Z_STAMP.match(result["window"]["since"]) and Z_STAMP.match(result["window"]["until"])
    assert result["window"]["hours"] == 6
    assert result["count"] == 2 and result["by_type"] == {"message": 2}
    assert len(result["items"]) == 1
    assert result["items"][0]["occurred_at"] == "2026-09-21T09:15:59Z"
    assert SHAPED not in json.dumps(body)


def test_finished_jobs_are_purged_after_fifteen_minutes(client, monkeypatch, clock):
    stub_dry_run(monkeypatch)
    job_id = start_dry_run(client)
    poll(client, job_id)

    clock["value"] = NOW + timedelta(minutes=14, seconds=59)
    assert client.get(f"/api/collectors/dry-run/{job_id}").status_code == 200

    clock["value"] = NOW + timedelta(minutes=15, seconds=1)
    assert client.get(f"/api/collectors/dry-run/{job_id}").status_code == 404
    assert collectors_router._jobs == {}


def test_a_job_that_never_finishes_is_purged_too(client, monkeypatch, clock, gate):
    stub_dry_run(monkeypatch, gate)
    job_id = start_dry_run(client)

    clock["value"] = NOW + timedelta(minutes=16)

    assert client.get(f"/api/collectors/dry-run/{job_id}").status_code == 404
    gate.set()
    time.sleep(0.05)  # the worker finishing a purged job must not resurrect it
    assert collectors_router._jobs == {}


def test_starting_a_job_purges_expired_ones(client, monkeypatch, clock):
    stub_dry_run(monkeypatch)
    old = start_dry_run(client)
    poll(client, old)

    clock["value"] = NOW + timedelta(minutes=20)
    fresh = start_dry_run(client)

    assert list(collectors_router._jobs) == [fresh]


def test_job_table_is_capped_and_running_jobs_are_never_evicted(client, monkeypatch, gate):
    stub_dry_run(monkeypatch, gate)
    job_ids = [start_dry_run(client) for _ in range(20)]

    full = client.post("/api/collectors/slack_work/dry-run", json={})

    assert full.status_code == 429 and code_of(full) == "too_many_dry_runs"
    assert len(collectors_router._jobs) == 20
    assert all(client.get(f"/api/collectors/dry-run/{job_id}").status_code == 200 for job_id in job_ids)


def test_a_full_table_of_finished_jobs_evicts_the_oldest(client, monkeypatch):
    stub_dry_run(monkeypatch)
    job_ids = [start_dry_run(client) for _ in range(20)]
    for job_id in job_ids:
        poll(client, job_id)

    newest = start_dry_run(client)

    assert len(collectors_router._jobs) == 20
    assert job_ids[0] not in collectors_router._jobs
    assert set(job_ids[1:]) | {newest} == set(collectors_router._jobs)
    assert client.get(f"/api/collectors/dry-run/{job_ids[0]}").status_code == 404


def test_the_cap_holds_under_concurrent_starts(client, monkeypatch, gate):
    stub_dry_run(monkeypatch, gate)

    def start(_: int) -> int:
        return client.post("/api/collectors/slack_work/dry-run", json={}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(start, range(40)))

    assert statuses.count(202) == 20 and statuses.count(429) == 20
    assert len(collectors_router._jobs) == 20


# --- POST /collectors/{source}/reset ----------------------------------------------------------


def test_reset_with_days_back_moves_the_watermark_and_clears_the_streak(client, clock):
    db.set_cursor("slack_work", NOW - timedelta(hours=1))
    db.record_failure("slack_work")

    response = client.post("/api/collectors/slack_work/reset", json={"days_back": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "slack_work" and body["watermark"] == "2026-09-18T10:05:30Z"
    assert "7 days" in body["note"] and "24 hours" in body["note"]
    row = cursor_of("slack_work")
    assert row.last_success_at == NOW - timedelta(days=3) and row.consecutive_failures == 0


@pytest.mark.parametrize("body", [{"days_back": None}, {}, None])
def test_reset_without_days_back_deletes_the_watermark(client, clock, body):
    db.set_cursor("slack_work", NOW - timedelta(hours=1))

    response = client.post("/api/collectors/slack_work/reset", json=body) if body is not None else client.post(
        "/api/collectors/slack_work/reset"
    )

    assert response.status_code == 200 and response.json()["watermark"] is None
    assert db.get_cursor("slack_work") is None


def test_reset_leaves_other_sources_alone(client, clock):
    db.set_cursor("slack_team", NOW - timedelta(hours=5))

    client.post("/api/collectors/slack_work/reset", json={"days_back": 2})

    assert cursor_of("slack_team").last_success_at == NOW - timedelta(hours=5)


@pytest.mark.parametrize("days_back", [0, 8, -1, "soon", 1.5])
def test_reset_rejects_days_back_outside_1_to_7(client, clock, days_back):
    db.set_cursor("slack_work", NOW - timedelta(hours=1))

    response = client.post("/api/collectors/slack_work/reset", json={"days_back": days_back})

    assert response.status_code == 422
    assert cursor_of("slack_work").last_success_at == NOW - timedelta(hours=1)


def test_reset_semantics_match_what_the_runner_does_with_the_watermark(client, clock):
    """The note promises: 30 minute overlap, floor at 7 days, a fresh watermark acts like none."""
    for days_back, expected in (
        (7, NOW - timedelta(days=7)),
        (3, NOW - timedelta(days=3, minutes=30)),
        (1, NOW - timedelta(days=1, minutes=30)),
    ):
        client.post("/api/collectors/slack_work/reset", json={"days_back": days_back})
        assert runner.collection_window("slack_work", NOW) == expected

    client.post("/api/collectors/slack_work/reset", json={})
    assert runner.collection_window("slack_work", NOW) == NOW - timedelta(hours=24)
    db.set_cursor("slack_work", NOW - timedelta(hours=2))
    assert runner.collection_window("slack_work", NOW) == NOW - timedelta(hours=24)


def test_reset_of_an_unknown_source_is_404(client, clock):
    db.set_cursor("slack_nope", NOW - timedelta(hours=1))

    response = client.post("/api/collectors/slack_nope/reset", json={"days_back": 2})

    assert response.status_code == 404 and code_of(response) == "unknown_source"
    assert cursor_of("slack_nope").last_success_at == NOW - timedelta(hours=1)


# --- POST /collectors/{source}/clear-failures -------------------------------------------------


def test_clear_failures_zeroes_the_streak_and_keeps_the_watermark(client):
    db.set_cursor("slack_work", NOW - timedelta(hours=30))
    db.record_failure("slack_work")
    db.record_failure("slack_work")
    db.record_failure("slack_work")

    response = client.post("/api/collectors/slack_work/clear-failures")

    assert response.status_code == 200 and response.json() == {"source": "slack_work", "consecutive_failures": 0}
    row = cursor_of("slack_work")
    assert row.consecutive_failures == 0 and row.last_success_at == NOW - timedelta(hours=30)


def test_clear_failures_with_no_cursor_is_a_no_op(client):
    response = client.post("/api/collectors/zoom/clear-failures")

    assert response.status_code == 200 and db.get_cursor("zoom") is None


def test_clear_failures_of_an_unknown_source_is_404(client):
    db.record_failure("slack_nope")

    response = client.post("/api/collectors/slack_nope/clear-failures")

    assert response.status_code == 404 and code_of(response) == "unknown_source"
    assert cursor_of("slack_nope").consecutive_failures == 1


# --- GET /collectors/{source}/items -----------------------------------------------------------


def test_items_are_paged_newest_first_with_next_before_id(client):
    ids = add_items("slack_work", *[("message", f"e{n}", {"text": f"item {n}"}) for n in range(5)])

    first = client.get("/api/collectors/slack_work/items", params={"limit": 2}).json()
    second = client.get("/api/collectors/slack_work/items", params={"limit": 2, "before_id": first["next_before_id"]}).json()
    third = client.get("/api/collectors/slack_work/items", params={"limit": 2, "before_id": second["next_before_id"]}).json()

    assert [i["id"] for i in first["items"]] == [ids[4], ids[3]] and first["next_before_id"] == ids[3]
    assert [i["id"] for i in second["items"]] == [ids[2], ids[1]] and second["next_before_id"] == ids[1]
    assert [i["id"] for i in third["items"]] == [ids[0]] and third["next_before_id"] is None
    assert first["total"] == second["total"] == third["total"] == 5


def test_an_exactly_full_last_page_has_no_next_page(client):
    add_items("slack_work", *[("message", f"e{n}", {"text": "x"}) for n in range(4)])

    first = client.get("/api/collectors/slack_work/items", params={"limit": 2}).json()
    last = client.get("/api/collectors/slack_work/items", params={"limit": 2, "before_id": first["next_before_id"]}).json()

    assert len(last["items"]) == 2 and last["next_before_id"] is None


def test_items_carry_a_preview_and_never_a_payload(client):
    add_items(
        "slack_work",
        ("message", "plain", "not json at all"),
        ("message", "listy", "[1, 2]"),
        ("mail", "mail1", {"subject": "Quarterly\nplan", "body": {"content": "long " * 100}}),
    )

    body = client.get("/api/collectors/slack_work/items").json()

    previews = {i["external_id"]: i["preview"] for i in body["items"]}
    assert previews == {"mail1": "Quarterly plan", "listy": "(no preview field)", "plain": "(no preview field)"}
    assert all(set(i) == {"id", "item_type", "external_id", "occurred_at", "preview"} for i in body["items"])
    assert all(Z_STAMP.match(i["occurred_at"]) for i in body["items"])
    assert "long long" not in json.dumps(body)


def test_items_filter_by_type_and_total_follows_the_filter(client):
    add_items(
        "slack_work",
        ("mail", "a", {"subject": "a"}),
        ("message", "b", {"text": "b"}),
        ("mail", "c", {"subject": "c"}),
    )

    body = client.get("/api/collectors/slack_work/items", params={"item_type": "mail"}).json()

    assert [i["external_id"] for i in body["items"]] == ["c", "a"] and body["total"] == 2


def test_items_never_include_another_sources_rows(client):
    add_items("slack_team", ("message", "team-1", {"text": "TEAM-ONLY-PAYLOAD"}))
    add_items("slack_work", ("message", "work-1", {"text": "work"}))

    work = client.get("/api/collectors/slack_work/items")
    team = client.get("/api/collectors/slack_team/items")
    empty = client.get("/api/collectors/zoom/items")

    assert [i["external_id"] for i in work.json()["items"]] == ["work-1"] and work.json()["total"] == 1
    assert "TEAM-ONLY-PAYLOAD" not in work.text and "team-1" not in work.text
    assert [i["external_id"] for i in team.json()["items"]] == ["team-1"]
    assert empty.json() == {"items": [], "next_before_id": None, "total": 0}


def test_item_previews_are_redacted(client):
    add_items("slack_work", ("message", "leaky", {"text": f"see {WORK_TOKEN} or {SHAPED}"}))

    response = client.get("/api/collectors/slack_work/items")

    assert MARKER in response.json()["items"][0]["preview"]
    assert WORK_TOKEN not in response.text and SHAPED not in response.text


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"before_id": 0}, {"limit": "many"}, {"before_id": "x"}])
def test_items_reject_bad_paging_arguments(client, params):
    assert client.get("/api/collectors/slack_work/items", params=params).status_code == 422


def test_items_of_an_unknown_source_is_404_even_when_rows_exist(client):
    add_items("slack_nope", ("message", "ghost", {"text": "ghost"}))

    response = client.get("/api/collectors/slack_nope/items")

    assert response.status_code == 404 and code_of(response) == "unknown_source"
    assert "ghost" not in response.text


# --- GET /collectors/{source}/items/{id} ------------------------------------------------------


def test_item_detail_returns_the_full_payload_as_pretty_text(client):
    payload = {"subject": "Plan", "nested": {"list": [1, 2, 3], "unicode": "café"}}
    (item_id,) = add_items("slack_work", ("mail", "m-1", payload))

    response = client.get(f"/api/collectors/slack_work/items/{item_id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "item_type", "external_id", "occurred_at", "payload", "truncated"}
    assert body["id"] == item_id and body["item_type"] == "mail" and body["external_id"] == "m-1"
    assert Z_STAMP.match(body["occurred_at"]) and body["truncated"] is False
    assert isinstance(body["payload"], str) and "café" in body["payload"]
    assert json.loads(body["payload"]) == payload


def test_item_detail_payload_is_bounded_to_20_kb(client):
    (big, multibyte, small) = add_items(
        "slack_work",
        ("mail", "big", {"blob": "x" * 50_000}),
        ("mail", "multi", {"blob": "é" * 30_000}),
        ("mail", "small", {"blob": "x" * 100}),
    )

    big_body = client.get(f"/api/collectors/slack_work/items/{big}").json()
    multi_body = client.get(f"/api/collectors/slack_work/items/{multibyte}").json()
    small_body = client.get(f"/api/collectors/slack_work/items/{small}").json()

    for body in (big_body, multi_body):
        assert body["truncated"] is True and len(body["payload"].encode("utf-8")) <= 20 * 1024
        assert len(body["payload"]) > 5000
    assert small_body["truncated"] is False


def test_item_detail_redacts_secrets(client):
    (item_id,) = add_items(
        "slack_work",
        ("message", "leaky", {"text": f"{WORK_TOKEN}", "nested": {"note": f"a {SHAPED} b", "team": TEAM_TOKEN}}),
    )

    response = client.get(f"/api/collectors/slack_work/items/{item_id}")

    assert response.status_code == 200 and MARKER in response.json()["payload"]
    for secret in (WORK_TOKEN, TEAM_TOKEN, SHAPED):
        assert secret not in response.text


def test_item_detail_redacts_before_cutting(client):
    """A secret straddling the 20 KB cut must not survive as a fragment."""
    padding = "p" * 20_446  # puts the token's first 8 characters just inside the 20480-byte cut
    (item_id,) = add_items("slack_work", ("message", "edge", {"pad": padding, "text": WORK_TOKEN}))

    body = client.get(f"/api/collectors/slack_work/items/{item_id}").json()

    assert body["truncated"] is True
    assert WORK_TOKEN[:8] not in body["payload"] and body["payload"].endswith(MARKER[:8])


def test_item_detail_of_a_non_json_payload_is_returned_as_text(client):
    (item_id,) = add_items("slack_work", ("message", "raw", f"plain text {WORK_TOKEN}"))

    body = client.get(f"/api/collectors/slack_work/items/{item_id}").json()

    assert body["payload"] == f"plain text {MARKER}"


def test_item_detail_of_another_sources_item_is_404_without_its_payload(client):
    (team_item,) = add_items("slack_team", ("message", "team-1", {"text": "TEAM-ONLY-PAYLOAD"}))

    response = client.get(f"/api/collectors/slack_work/items/{team_item}")

    assert response.status_code == 404 and code_of(response) == "item_not_found"
    assert "TEAM-ONLY-PAYLOAD" not in response.text


def test_item_detail_missing_id_and_unknown_source_are_404(client):
    (item_id,) = add_items("slack_work", ("message", "m", {"text": "x"}))

    missing = client.get(f"/api/collectors/slack_work/items/{item_id + 100}")
    unknown = client.get(f"/api/collectors/slack_nope/items/{item_id}")

    assert missing.status_code == 404 and code_of(missing) == "item_not_found"
    assert unknown.status_code == 404 and code_of(unknown) == "unknown_source"
    assert client.get("/api/collectors/slack_work/items/not-a-number").status_code == 422


# --- request guard ----------------------------------------------------------------------------


def test_mutating_routes_need_the_required_header(client):
    bare = TestClient(client.app)

    for path in ("run", "dry-run", "reset", "clear-failures"):
        assert bare.post(f"/api/collectors/slack_work/{path}").status_code == 403
