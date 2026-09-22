"""
Tests for api.scheduler's second job: domain sync + snapshot refresh (T-014).

sync_all()/refresh_snapshots() are stubbed at api.scheduler's own import site (module convention:
stub outbound calls where the module under test looked them up), never mocked as a module. The
scheduler is never started (add_job/reschedule_job/get_job all work on a stopped
BackgroundScheduler), so no test needs a real background thread.
"""
import logging

from apscheduler.triggers.cron import CronTrigger

from api import scheduler
from pipeline import health
from pipeline.domains import DEFAULT_REFRESH_CRON


def test_domains_job_id_constant():
    assert scheduler.DOMAINS_JOB_ID == "domains_refresh"


def test_run_domains_scheduled_calls_sync_then_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "sync_all", lambda: calls.append("sync_all"))
    monkeypatch.setattr(scheduler, "refresh_snapshots", lambda: calls.append("refresh_snapshots"))

    scheduler._run_domains_scheduled()

    assert calls == ["sync_all", "refresh_snapshots"]


def test_run_domains_scheduled_swallows_and_logs_exception_class(monkeypatch, caplog):
    def boom():
        raise RuntimeError("registrar credential leaked here")

    monkeypatch.setattr(scheduler, "sync_all", boom)
    monkeypatch.setattr(scheduler, "refresh_snapshots", lambda: None)

    with caplog.at_level(logging.WARNING, logger="api.scheduler"):
        scheduler._run_domains_scheduled()  # must not raise

    assert "RuntimeError" in caplog.text
    assert "registrar credential leaked here" not in caplog.text


def test_domains_trigger_uses_configured_cron():
    with health.env_override({"DOMAINS_REFRESH_CRON": "15 4 * * *"}):
        trigger = scheduler._domains_trigger()
    assert isinstance(trigger, CronTrigger)
    expected = CronTrigger.from_crontab("15 4 * * *", timezone=scheduler.schedule_timezone() or scheduler.UTC)
    assert str(trigger) == str(expected)


def test_domains_trigger_falls_back_to_default_on_invalid_cron(caplog):
    with health.env_override({"DOMAINS_REFRESH_CRON": "not a cron"}):
        with caplog.at_level(logging.WARNING, logger="api.scheduler"):
            trigger = scheduler._domains_trigger()
    expected = CronTrigger.from_crontab(DEFAULT_REFRESH_CRON, timezone=scheduler.schedule_timezone() or scheduler.UTC)
    assert str(trigger) == str(expected)
    assert "invalid DOMAINS_REFRESH_CRON" in caplog.text


def test_reschedule_domains_adds_then_reschedules_job():
    try:
        assert scheduler._scheduler.get_job(scheduler.DOMAINS_JOB_ID) is None
        scheduler.reschedule_domains()
        job = scheduler._scheduler.get_job(scheduler.DOMAINS_JOB_ID)
        assert job is not None
        assert job.func is scheduler._run_domains_scheduled

        # Calling again must reschedule the existing job, not duplicate it.
        scheduler.reschedule_domains()
        assert scheduler._scheduler.get_job(scheduler.DOMAINS_JOB_ID) is job
    finally:
        if scheduler._scheduler.get_job(scheduler.DOMAINS_JOB_ID):
            scheduler._scheduler.remove_job(scheduler.DOMAINS_JOB_ID)


def test_next_domains_run_time_none_when_unscheduled():
    assert scheduler._scheduler.get_job(scheduler.DOMAINS_JOB_ID) is None
    assert scheduler.next_domains_run_time() is None
