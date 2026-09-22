"""
Tests for the timestamps api.routers.runs sends (check finding 8). Every stored datetime is naive
UTC; serialized bare it has no zone and a browser reads it as LOCAL time, so the UI showed times off
by the viewer's UTC offset. Router-only app on a temp DB, never entered as a context manager so the
real lifespan (and data/digest.db) is not touched. Nothing is mocked: the rows go in through
pipeline.db and come back through the real routes.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import runs as runs_router  # noqa: E402
from pipeline import db  # noqa: E402

Z_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
STARTED = datetime(2026, 9, 21, 10, 5, 30)
FINISHED = datetime(2026, 9, 21, 10, 7, 45)
CHECKED = datetime(2026, 9, 21, 10, 6, 15)


def parse_z(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(temp_db):
    app = FastAPI()
    app.include_router(runs_router.router, prefix="/api")
    return TestClient(app)


def add_run(**fields) -> int:
    with db.get_session() as session:
        run = db.Run(**fields)
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        return run.id


def add_health(run_id: int, source: str, checked_at: datetime) -> None:
    with db.get_session() as session:
        session.add(db.SourceHealth(run_id=run_id, source=source, status="ok", checked_at=checked_at))
        session.commit()


def test_list_timestamps_end_with_z_and_round_trip_to_the_stored_utc_value(client):
    run_id = add_run(trigger="manual", status="success", started_at=STARTED, finished_at=FINISHED)

    rows = client.get("/api/runs").json()

    assert [row["id"] for row in rows] == [run_id]
    assert rows[0]["started_at"] == "2026-09-21T10:05:30Z"
    assert rows[0]["finished_at"] == "2026-09-21T10:07:45Z"
    assert Z_STAMP.match(rows[0]["started_at"]) and Z_STAMP.match(rows[0]["finished_at"])
    assert parse_z(rows[0]["started_at"]) == STARTED
    assert parse_z(rows[0]["finished_at"]) == FINISHED


def test_detail_timestamps_end_with_z_and_source_health_checked_at_too(client):
    run_id = add_run(trigger="scheduled", status="success", started_at=STARTED, finished_at=FINISHED)
    add_health(run_id, "slack_work", CHECKED)

    body = client.get(f"/api/runs/{run_id}").json()

    assert Z_STAMP.match(body["started_at"]) and Z_STAMP.match(body["finished_at"])
    assert parse_z(body["started_at"]) == STARTED
    assert parse_z(body["finished_at"]) == FINISHED
    assert [h["source"] for h in body["source_health"]] == ["slack_work"]
    checked_at = body["source_health"][0]["checked_at"]
    assert checked_at.endswith("Z") and Z_STAMP.match(checked_at)
    assert parse_z(checked_at) == CHECKED


def test_running_run_has_null_finished_at_in_list_and_detail(client):
    run_id = add_run(trigger="manual", status="running", started_at=STARTED)

    listed = client.get("/api/runs").json()[0]
    detail = client.get(f"/api/runs/{run_id}").json()

    for body in (listed, detail):
        assert body["status"] == "running"
        assert body["finished_at"] is None
        assert body["started_at"] == "2026-09-21T10:05:30Z"


def test_payload_shape_is_unchanged(client):
    run_id = add_run(trigger="manual", status="failed", started_at=STARTED, finished_at=FINISHED, error="boom")
    add_health(run_id, "zoom", CHECKED)

    listed = client.get("/api/runs").json()[0]
    detail = client.get(f"/api/runs/{run_id}").json()

    keys = {"id", "trigger", "status", "started_at", "finished_at", "summary", "error", "has_pdf"}
    assert set(listed) == keys
    assert set(detail) == keys | {"source_health"}
    assert set(detail["source_health"][0]) == {"source", "status", "detail", "checked_at"}
    assert (listed["trigger"], listed["status"], listed["error"], listed["has_pdf"]) == ("manual", "failed", "boom", False)
