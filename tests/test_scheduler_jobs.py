"""
Tests for api.scheduler's third job: jobs-collector resolve/poll/score (T-018).

resolve_pending()/poll_all()/score_new() are stubbed at api.scheduler's own import site (module
convention: stub outbound calls where the module under test looked them up, per
tests/test_scheduler_domains.py), never mocked as a module. db.get_session() is replaced with a
fake context manager wrapping a fake session whose exec().first() returns a preset JobsProfile (or
None) so no real DB file is touched. The scheduler is never started (add_job/reschedule_job/get_job
all work on a stopped BackgroundScheduler), so no test needs a real background thread.
"""
import logging

from apscheduler.triggers.cron import CronTrigger

from api import scheduler
from pipeline import health
from pipeline.db import JobsProfile
from pipeline.jobs import DEFAULT_REFRESH_CRON as JOBS_DEFAULT_REFRESH_CRON


class _FakeExecResult:
    def __init__(self, first_value):
        self._first_value = first_value

    def first(self):
        return self._first_value


class _FakeSession:
    def __init__(self, profile):
        self._profile = profile

    def exec(self, _statement):
        return _FakeExecResult(self._profile)


class _FakeSessionCtx:
    def __init__(self, profile):
        self._session = _FakeSession(profile)

    def __enter__(self):
        return self._session

    def __exit__(self, *exc_info):
        return False


class _FakeDb:
    """Stands in for the `db` name scheduler imports via `from pipeline import db`."""

    def __init__(self, profile=None):
        self._profile = profile

    def get_session(self):
        return _FakeSessionCtx(self._profile)


def test_jobs_job_id_constant():
    assert scheduler.JOBS_JOB_ID == "jobs_refresh"


def test_run_jobs_scheduled_calls_resolve_then_poll_then_score_when_profile_exists(monkeypatch):
    calls = []
    profile = JobsProfile()
    monkeypatch.setattr(scheduler, "db", _FakeDb(profile=profile))
    monkeypatch.setattr(scheduler, "resolve_pending", lambda session: calls.append(("resolve_pending", session)))
    monkeypatch.setattr(scheduler, "poll_all", lambda session: calls.append(("poll_all", session)))
    monkeypatch.setattr(
        scheduler, "score_new", lambda session, given_profile: calls.append(("score_new", session, given_profile))
    )

    scheduler._run_jobs_scheduled()

    assert [call[0] for call in calls] == ["resolve_pending", "poll_all", "score_new"]
    # Every step shares the same session (opened once for the whole run).
    sessions = {call[1] for call in calls}
    assert len(sessions) == 1
    assert calls[2][2] is profile


def test_run_jobs_scheduled_skips_score_new_without_a_profile(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(scheduler, "db", _FakeDb(profile=None))
    monkeypatch.setattr(scheduler, "resolve_pending", lambda session: calls.append("resolve_pending"))
    monkeypatch.setattr(scheduler, "poll_all", lambda session: calls.append("poll_all"))

    def fail_score_new(session, profile):
        raise AssertionError("score_new must not be called without a JobsProfile")

    monkeypatch.setattr(scheduler, "score_new", fail_score_new)

    with caplog.at_level(logging.INFO, logger="api.scheduler"):
        scheduler._run_jobs_scheduled()  # must not raise

    assert calls == ["resolve_pending", "poll_all"]
    assert "no JobsProfile" in caplog.text


def test_run_jobs_scheduled_swallows_and_logs_exception_class(monkeypatch, caplog):
    def boom(session):
        raise RuntimeError("api key leaked here")

    monkeypatch.setattr(scheduler, "db", _FakeDb(profile=None))
    monkeypatch.setattr(scheduler, "resolve_pending", boom)
    monkeypatch.setattr(scheduler, "poll_all", lambda session: None)
    monkeypatch.setattr(scheduler, "score_new", lambda session, profile: None)

    with caplog.at_level(logging.WARNING, logger="api.scheduler"):
        scheduler._run_jobs_scheduled()  # must not raise

    assert "RuntimeError" in caplog.text
    assert "api key leaked here" not in caplog.text


def test_jobs_trigger_uses_configured_cron():
    with health.env_override({"JOBS_REFRESH_CRON": "30 5 * * *"}):
        trigger = scheduler._jobs_trigger()
    assert isinstance(trigger, CronTrigger)
    expected = CronTrigger.from_crontab("30 5 * * *", timezone=scheduler.schedule_timezone() or scheduler.UTC)
    assert str(trigger) == str(expected)


def test_jobs_trigger_falls_back_to_default_on_invalid_cron(caplog):
    with health.env_override({"JOBS_REFRESH_CRON": "not a cron"}):
        with caplog.at_level(logging.WARNING, logger="api.scheduler"):
            trigger = scheduler._jobs_trigger()
    expected = CronTrigger.from_crontab(JOBS_DEFAULT_REFRESH_CRON, timezone=scheduler.schedule_timezone() or scheduler.UTC)
    assert str(trigger) == str(expected)
    assert "invalid JOBS_REFRESH_CRON" in caplog.text


def test_reschedule_jobs_adds_then_reschedules_job():
    try:
        assert scheduler._scheduler.get_job(scheduler.JOBS_JOB_ID) is None
        scheduler.reschedule_jobs()
        job = scheduler._scheduler.get_job(scheduler.JOBS_JOB_ID)
        assert job is not None
        assert job.func is scheduler._run_jobs_scheduled

        # Calling again must reschedule the existing job, not duplicate it.
        scheduler.reschedule_jobs()
        assert scheduler._scheduler.get_job(scheduler.JOBS_JOB_ID) is job
    finally:
        if scheduler._scheduler.get_job(scheduler.JOBS_JOB_ID):
            scheduler._scheduler.remove_job(scheduler.JOBS_JOB_ID)


def test_next_jobs_run_time_none_when_unscheduled():
    assert scheduler._scheduler.get_job(scheduler.JOBS_JOB_ID) is None
    assert scheduler.next_jobs_run_time() is None
