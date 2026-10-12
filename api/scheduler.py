"""
One BackgroundScheduler, four cron jobs: the daily digest run (cron from config.json, re-read on
every reschedule() so the config editor's schedule field takes effect without a restart) and the
domains, jobs and packages refreshes (cron from .env, read at startup).

The cron expression is interpreted in TZ (from .env), not the container's clock. A container with
no TZ set runs UTC, so "0 6 * * *" meant as 6am local would fire at 6am UTC — the wrong hour, and a
24h window covering the wrong day.
"""
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import dotenv_values
from sqlmodel import select

from pipeline import db
from pipeline.config_store import STATIC_DEFAULTS, load_config
from pipeline.db import JobsProfile
from pipeline.domains import DEFAULT_REFRESH_CRON, domain_settings
from pipeline.domains.inventory import refresh_snapshots, sync_all
from pipeline.jobs import DEFAULT_REFRESH_CRON as JOBS_DEFAULT_REFRESH_CRON
from pipeline.jobs import jobs_settings
from pipeline.jobs.inventory import poll_all, resolve_pending
from pipeline.jobs.scoring import score_new
from pipeline.packages import DEFAULT_REFRESH_CRON as PACKAGES_DEFAULT_REFRESH_CRON
from pipeline.packages import packages_settings
from pipeline.packages.poll import refresh_due
from pipeline.packages.scan import scan_all
from pipeline.runner import RunAlreadyInProgress, execute_run

_log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
UTC = ZoneInfo("UTC")


def schedule_timezone():
    """
    ZoneInfo for TZ, or None when TZ is unset or unparseable.

    Read from .env as well as the environment so local dev (uvicorn, no exported TZ) matches the
    container, where docker-compose's env_file puts it in the environment.
    """
    name = os.environ.get("TZ") or dotenv_values(ROOT / ".env").get("TZ")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A typo'd TZ must not take the scheduler down; fall back and keep running.
        return None


# Explicit timezone, never inferred. Left to itself APScheduler asks tzlocal to guess from the
# system clock, which warns and picks something arbitrary when a container's /etc/localtime and its
# actual offset disagree. Only a default for jobs that don't carry their own zone — reschedule()
# always sets one on the trigger — so UTC here is a safe floor, not the schedule.
_scheduler = BackgroundScheduler(timezone=schedule_timezone() or UTC)
JOB_ID = "daily_digest_run"
DOMAINS_JOB_ID = "domains_refresh"
JOBS_JOB_ID = "jobs_refresh"
PACKAGES_JOB_ID = "packages_refresh"


def _run_scheduled() -> None:
    try:
        execute_run(trigger="scheduled")
    except RunAlreadyInProgress:
        # A manual run is still going. Skip this tick rather than letting APScheduler log a
        # traceback — the next cron fire picks it up.
        pass


def _run_domains_scheduled() -> None:
    """
    Registrar sync followed by a snapshot refresh, both run inline so a snapshot always reflects
    the domain list sync_all() just wrote. Any failure is caught here (never left to APScheduler's
    own traceback logging) and logged by exception CLASS only, per pipeline/redact.py: a registrar
    or DNS error can echo back submitted credentials.
    """
    try:
        sync_all()
        refresh_snapshots()
    except Exception as exc:
        _log.warning("domains scheduled refresh failed: %s", type(exc).__name__)


def _run_jobs_scheduled() -> None:
    """
    Resolve -> poll -> score, run inline in one session so a scoring pass always sees postings this
    run's own poll just upserted. Any failure is caught here (never left to APScheduler's own
    traceback logging) and logged by exception CLASS only, per pipeline/redact.py: a resolver/ATS/
    LLM error can echo request/response detail.

    Scoring is skipped (not a failure) when no JobsProfile row exists yet: a fresh install with the
    profile never configured is a normal state, not something to warn about.
    """
    try:
        with db.get_session() as session:
            resolve_pending(session)
            poll_all(session)
            profile = session.exec(select(JobsProfile)).first()
            if profile is not None:
                score_new(session, profile)
            else:
                _log.info("jobs scheduled refresh: no JobsProfile configured yet, skipping scoring")
    except Exception as exc:
        _log.warning("jobs scheduled refresh failed: %s", type(exc).__name__)


def _run_packages_scheduled() -> None:
    """
    Mailbox scan followed by a carrier poll, each in its own try block so a scan failure still lets
    the poll run (tracked packages keep refreshing even when Gmail is down). Failures are logged by
    exception CLASS only, per pipeline/redact.py: a Gmail/carrier error can echo tokens or detail.
    """
    try:
        scan_all()
    except Exception as exc:
        _log.warning("packages scheduled scan failed: %s", type(exc).__name__)
    try:
        refresh_due()
    except Exception as exc:
        _log.warning("packages scheduled poll failed: %s", type(exc).__name__)


def _cron_trigger(cron: str, default: str, env_name: str) -> CronTrigger:
    """
    CronTrigger for `cron` in the schedule timezone. A value that fails cron parsing (not just the
    already-defaulted empty case each settings() reader handles) falls back to `default` with a
    warning naming `env_name` rather than taking the scheduler down.
    """
    # "or UTC" is load-bearing: CronTrigger with timezone=None calls get_localzone() itself and
    # does NOT inherit the scheduler's default, because add_job receives a built trigger instance.
    tz = schedule_timezone() or UTC
    try:
        return CronTrigger.from_crontab(cron, timezone=tz)
    except ValueError:
        _log.warning("invalid %s %r, using default %r", env_name, cron, default)
        return CronTrigger.from_crontab(default, timezone=tz)


def _upsert_job(job_id: str, func, trigger: CronTrigger) -> None:
    if _scheduler.get_job(job_id):
        _scheduler.reschedule_job(job_id, trigger=trigger)
    else:
        _scheduler.add_job(func, trigger=trigger, id=job_id, replace_existing=True)


def reschedule() -> None:
    """Digest job from config.json's schedule_cron; an unparseable value falls back to the default."""
    config = load_config()
    _upsert_job(JOB_ID, _run_scheduled, _cron_trigger(config["schedule_cron"], STATIC_DEFAULTS["schedule_cron"], "schedule_cron"))


def _domains_trigger() -> CronTrigger:
    return _cron_trigger(domain_settings().refresh_cron, DEFAULT_REFRESH_CRON, "DOMAINS_REFRESH_CRON")


def reschedule_domains() -> None:
    _upsert_job(DOMAINS_JOB_ID, _run_domains_scheduled, _domains_trigger())


def _jobs_trigger() -> CronTrigger:
    return _cron_trigger(jobs_settings().refresh_cron, JOBS_DEFAULT_REFRESH_CRON, "JOBS_REFRESH_CRON")


def reschedule_jobs() -> None:
    _upsert_job(JOBS_JOB_ID, _run_jobs_scheduled, _jobs_trigger())


def _packages_trigger() -> CronTrigger:
    return _cron_trigger(packages_settings().refresh_cron, PACKAGES_DEFAULT_REFRESH_CRON, "PACKAGES_REFRESH_CRON")


def reschedule_packages() -> None:
    _upsert_job(PACKAGES_JOB_ID, _run_packages_scheduled, _packages_trigger())


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
    reschedule()
    reschedule_domains()
    reschedule_jobs()
    reschedule_packages()


def next_run_time(job_id: str = JOB_ID):
    """
    The job's next fire time (aware datetime), or None when it is not scheduled. A job added to a
    scheduler that has not started yet has no next_run_time attribute at all (APScheduler sets it on
    start), so that case reads as None too.
    """
    job = _scheduler.get_job(job_id)
    return getattr(job, "next_run_time", None) if job else None
