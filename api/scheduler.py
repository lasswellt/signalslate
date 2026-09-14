"""
One BackgroundScheduler job, re-read from config on every (re)schedule call so the config
editor's schedule field takes effect without a container restart.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from pipeline.config_store import load_config
from pipeline.runner import RunAlreadyInProgress, execute_run

_scheduler = BackgroundScheduler()
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
    trigger = CronTrigger.from_crontab(config["schedule_cron"])
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
