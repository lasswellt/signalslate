from fastapi import APIRouter

from api.scheduler import next_run_time
from pipeline.db import latest_run, latest_source_health

router = APIRouter(tags=["status"])


@router.get("/status")
def get_status():
    run = latest_run()
    health = latest_source_health()

    return {
        "last_run": None
        if run is None
        else {
            "id": run.id,
            "trigger": run.trigger,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "summary": run.summary,
            "error": run.error,
        },
        "next_scheduled_run": next_run_time(),
        "source_health": [
            {
                "source": h.source,
                "status": h.status,
                "detail": h.detail,
                "checked_at": h.checked_at,
            }
            for h in health
        ],
    }
