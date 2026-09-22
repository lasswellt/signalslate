"""
One BackgroundScheduler job, re-read from config on every (re)schedule call so the config
editor's schedule field takes effect without a container restart.

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

from pipeline.config_store import load_config
from pipeline.domains import DEFAULT_REFRESH_CRON, domain_settings
from pipeline.domains.inventory import refresh_snapshots, sync_all
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


def reschedule() -> None:
    config = load_config()
    # "or UTC" is load-bearing: CronTrigger with timezone=None calls get_localzone() itself and
    # does NOT inherit the scheduler's default, because add_job receives a built trigger instance.
    trigger = CronTrigger.from_crontab(config["schedule_cron"], timezone=schedule_timezone() or UTC)
    if _scheduler.get_job(JOB_ID):
        _scheduler.reschedule_job(JOB_ID, trigger=trigger)
    else:
        _scheduler.add_job(_run_scheduled, trigger=trigger, id=JOB_ID, replace_existing=True)


def _domains_trigger() -> CronTrigger:
    """CronTrigger from domain_settings().refresh_cron; a value that fails cron parsing (not just
    the already-defaulted empty case domain_settings() handles) falls back to DEFAULT_REFRESH_CRON
    with a warning rather than taking the scheduler down."""
    cron = domain_settings().refresh_cron
    try:
        return CronTrigger.from_crontab(cron, timezone=schedule_timezone() or UTC)
    except ValueError:
        _log.warning("invalid DOMAINS_REFRESH_CRON %r, using default %r", cron, DEFAULT_REFRESH_CRON)
        return CronTrigger.from_crontab(DEFAULT_REFRESH_CRON, timezone=schedule_timezone() or UTC)


def reschedule_domains() -> None:
    trigger = _domains_trigger()
    if _scheduler.get_job(DOMAINS_JOB_ID):
        _scheduler.reschedule_job(DOMAINS_JOB_ID, trigger=trigger)
    else:
        _scheduler.add_job(_run_domains_scheduled, trigger=trigger, id=DOMAINS_JOB_ID, replace_existing=True)


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
    reschedule()
    reschedule_domains()


def next_run_time():
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None


def next_domains_run_time():
    job = _scheduler.get_job(DOMAINS_JOB_ID)
    return job.next_run_time if job else None
