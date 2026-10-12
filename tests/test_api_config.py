"""
Tests for PUT /api/config's cron validation and api.scheduler.reschedule()'s fallback.

Before these, an unparseable schedule_cron was persisted to config.json first and only then failed
in reschedule() (a 500 for the caller), after which every startup re-read the bad value and failed
in lifespan: with `restart: unless-stopped` the container crash-looped. The router now rejects it
with 422 before saving, and reschedule() falls back to the default with a warning, so a bad value
that reaches config.json by another path (a hand edit) still lets the app start.
"""
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import scheduler  # noqa: E402
from api.routers import config as config_router  # noqa: E402
from pipeline import config_store  # noqa: E402

VALID = {"schedule_cron": "0 7 * * *", "tracker": "none", "active_sources": {}}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config_store, "_defaults", lambda: {**config_store.STATIC_DEFAULTS, "active_sources": {}, "connection_active": {}})
    reschedules = []
    monkeypatch.setattr(config_router, "reschedule", lambda: reschedules.append(1))
    app = FastAPI()
    app.include_router(config_router.router, prefix="/api")
    return TestClient(app), reschedules, tmp_path / "config.json"


def test_invalid_cron_is_rejected_before_anything_is_saved(client):
    http, reschedules, path = client

    resp = http.put("/api/config", json={**VALID, "schedule_cron": "not a cron"})

    assert resp.status_code == 422
    assert "schedule_cron" in json.dumps(resp.json())
    assert not path.exists()
    assert reschedules == []


def test_valid_cron_is_saved_and_rescheduled(client):
    http, reschedules, path = client

    resp = http.put("/api/config", json=VALID)

    assert resp.status_code == 200
    assert resp.json()["schedule_cron"] == "0 7 * * *"
    assert json.loads(path.read_text())["schedule_cron"] == "0 7 * * *"
    assert reschedules == [1]


def test_reschedule_falls_back_to_default_on_a_bad_stored_cron(monkeypatch, caplog):
    monkeypatch.setattr(scheduler, "load_config", lambda: {"schedule_cron": "not a cron"})

    try:
        with caplog.at_level(logging.WARNING, logger="api.scheduler"):
            scheduler.reschedule()  # must not raise

        assert "invalid schedule_cron" in caplog.text
        job = scheduler._scheduler.get_job(scheduler.JOB_ID)
        assert job is not None
        expected = scheduler.CronTrigger.from_crontab(
            config_store.STATIC_DEFAULTS["schedule_cron"], timezone=scheduler.schedule_timezone() or scheduler.UTC
        )
        assert str(job.trigger) == str(expected)
        assert scheduler.next_run_time() is None  # pending on a stopped scheduler, never an AttributeError
    finally:
        # The scheduler is module-global and never started in tests; leave no digest job behind for
        # the /api/status tests that run after this file.
        scheduler._scheduler.remove_job(scheduler.JOB_ID)
