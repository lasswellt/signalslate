from fastapi import APIRouter
from pydantic import BaseModel

from api.scheduler import reschedule
from pipeline.config_store import load_config, save_config

router = APIRouter(tags=["config"])


class ConfigUpdate(BaseModel):
    schedule_cron: str
    tracker: str
    active_sources: dict[str, bool]


@router.get("/config")
def get_config():
    return load_config()


@router.put("/config")
def update_config(update: ConfigUpdate):
    saved = save_config(update.model_dump())
    reschedule()  # picks up the new schedule_cron immediately
    return saved
