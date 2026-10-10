"""
Tests for api.scheduler's packages job: mailbox scan then carrier poll.

scan_all()/refresh_due() are stubbed at api.scheduler's own import site, and the real scheduler
thread is never started (start_scheduler is exercised with _scheduler.start/reschedule* stubbed).
"""
import logging

from apscheduler.triggers.cron import CronTrigger

from api import scheduler
from pipeline import health
from pipeline.packages import DEFAULT_REFRESH_CRON


def test_packages_job_id_constant():
    assert scheduler.PACKAGES_JOB_ID == "packages_refresh"


def test_run_packages_scheduled_scans_then_polls(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "scan_all", lambda: calls.append("scan_all"))
    monkeypatch.setattr(scheduler, "refresh_due", lambda: calls.append("refresh_due"))

    scheduler._run_packages_scheduled()

    assert calls == ["scan_all", "refresh_due"]


def test_scan_failure_still_polls_and_logs_class_only(monkeypatch, caplog):
    calls = []

    def boom():
        raise RuntimeError("gmail token leaked here")

    monkeypatch.setattr(scheduler, "scan_all", boom)
    monkeypatch.setattr(scheduler, "refresh_due", lambda: calls.append("refresh_due"))

    with caplog.at_level(logging.WARNING, logger="api.scheduler"):
        scheduler._run_packages_scheduled()  # must not raise

    assert calls == ["refresh_due"]
    assert "RuntimeError" in caplog.text
    assert "gmail token leaked here" not in caplog.text


def test_poll_failure_is_swallowed(monkeypatch, caplog):
    def boom():
        raise ValueError("carrier secret")

    monkeypatch.setattr(scheduler, "scan_all", lambda: None)
    monkeypatch.setattr(scheduler, "refresh_due", boom)

    with caplog.at_level(logging.WARNING, logger="api.scheduler"):
        scheduler._run_packages_scheduled()

    assert "ValueError" in caplog.text
    assert "carrier secret" not in caplog.text


def test_packages_trigger_uses_configured_cron():
    with health.env_override({"PACKAGES_REFRESH_CRON": "15 4 * * *"}):
        trigger = scheduler._packages_trigger()
    assert isinstance(trigger, CronTrigger)
    expected = CronTrigger.from_crontab("15 4 * * *", timezone=scheduler.schedule_timezone() or scheduler.UTC)
    assert str(trigger) == str(expected)


def test_packages_trigger_falls_back_to_default_on_invalid_cron(monkeypatch, caplog):
    class _Bad:
        refresh_cron = "99 99 * * *"

    monkeypatch.setattr(scheduler, "packages_settings", lambda: _Bad())
    with caplog.at_level(logging.WARNING, logger="api.scheduler"):
        trigger = scheduler._packages_trigger()
    expected = CronTrigger.from_crontab(DEFAULT_REFRESH_CRON, timezone=scheduler.schedule_timezone() or scheduler.UTC)
    assert str(trigger) == str(expected)
    assert "invalid PACKAGES_REFRESH_CRON" in caplog.text


def test_reschedule_packages_adds_then_reschedules_job():
    try:
        assert scheduler._scheduler.get_job(scheduler.PACKAGES_JOB_ID) is None
        scheduler.reschedule_packages()
        job = scheduler._scheduler.get_job(scheduler.PACKAGES_JOB_ID)
        assert job is not None
        assert job.func is scheduler._run_packages_scheduled
        scheduler.reschedule_packages()
        assert scheduler._scheduler.get_job(scheduler.PACKAGES_JOB_ID) is job
    finally:
        if scheduler._scheduler.get_job(scheduler.PACKAGES_JOB_ID):
            scheduler._scheduler.remove_job(scheduler.PACKAGES_JOB_ID)


def test_start_scheduler_registers_packages_job(monkeypatch):
    called = []
    monkeypatch.setattr(scheduler._scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "reschedule", lambda: None)
    monkeypatch.setattr(scheduler, "reschedule_domains", lambda: None)
    monkeypatch.setattr(scheduler, "reschedule_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "reschedule_packages", lambda: called.append("packages"))

    scheduler.start_scheduler()

    assert called == ["packages"]


def test_next_packages_run_time_none_when_unscheduled():
    assert scheduler._scheduler.get_job(scheduler.PACKAGES_JOB_ID) is None
    assert scheduler.next_packages_run_time() is None
