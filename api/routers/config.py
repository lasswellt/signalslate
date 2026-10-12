from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter
from pydantic import BaseModel, field_validator

from api.scheduler import reschedule
from pipeline.config_store import load_config, save_config

router = APIRouter(tags=["config"])


class ConfigUpdate(BaseModel):
    schedule_cron: str
    tracker: str
    active_sources: dict[str, bool]
    connection_active: Optional[dict[str, bool]] = None

    @field_validator("schedule_cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        # Rejected here (422) rather than persisted: config.json is read again at every startup, and
        # an unparseable cron there would otherwise fail the scheduler, and the app, on each boot.
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"schedule_cron is not a valid 5-field cron expression: {exc}") from None
        return value


@router.get("/config")
def get_config():
    return load_config()


@router.put("/config")
def update_config(update: ConfigUpdate):
    payload = update.model_dump()
    if payload["connection_active"] is None:  # older clients omit it; keep what is stored
        payload["connection_active"] = load_config().get("connection_active", {})
    saved = save_config(payload)
    reschedule()  # picks up the new schedule_cron immediately
    return saved
