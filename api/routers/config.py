from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from api.scheduler import reschedule
from pipeline.config_store import load_config, save_config

router = APIRouter(tags=["config"])


class ConfigUpdate(BaseModel):
    schedule_cron: str
    tracker: str
    active_sources: dict[str, bool]
    connection_active: Optional[dict[str, bool]] = None


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
