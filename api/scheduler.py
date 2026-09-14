"""
One BackgroundScheduler job, re-read from config on every (re)schedule call so the config
editor's schedule field takes effect without a container restart.

The cron expression is interpreted in TZ (from .env), not the container's clock. A container with
no TZ set runs UTC, so "0 6 * * *" meant as 6am local would fire at 6am UTC — the wrong hour, and a
24h window covering the wrong day.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import dotenv_values

from pipeline.config_store import load_config
from pipeline.runner import RunAlreadyInProgress, execute_run

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


def _run_scheduled() -> None:
    try:
        execute_run(trigger="scheduled")
    except RunAlreadyInProgress:
        # A manual run is still going. Skip this tick rather than letting APScheduler log a
        # traceback — the next cron fire picks it up.
        pass


def reschedule() -> None:
    config = load_config()
    # "or UTC" is load-bearing: CronTrigger with timezone=None calls get_localzone() itself and
    # does NOT inherit the scheduler's default, because add_job receives a built trigger instance.
    trigger = CronTrigger.from_crontab(config["schedule_cron"], timezone=schedule_timezone() or UTC)
    if _scheduler.get_job(JOB_ID):
        _scheduler.reschedule_job(JOB_ID, trigger=trigger)
    else:
        _scheduler.add_job(_run_scheduled, trigger=trigger, id=JOB_ID, replace_existing=True)


def start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
    reschedule()


def next_run_time():
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None
