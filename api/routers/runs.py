from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from pipeline.db import get_run, has_running_run, list_runs, source_health_for_run
from pipeline.runner import execute_run

router = APIRouter(tags=["runs"])


def _run_to_dict(run) -> dict:
    return {
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "summary": run.summary,
        "error": run.error,
        "has_pdf": bool(run.pdf_path),
    }


@router.get("/runs")
def get_runs(limit: int = 50):
    return [_run_to_dict(r) for r in list_runs(limit=limit)]


@router.get("/runs/{run_id}")
def get_run_detail(run_id: int):
    run = get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return {
        **_run_to_dict(run),
        "source_health": [
            {"source": h.source, "status": h.status, "detail": h.detail, "checked_at": h.checked_at}
            for h in source_health_for_run(run_id)
        ],
    }


@router.post("/runs/trigger")
def trigger_run(background_tasks: BackgroundTasks):
    # Checked here as well as inside execute_run: an exception raised in a background task is
    # swallowed by Starlette, so the user would get {"accepted": true} and no run.
    if has_running_run():
        raise HTTPException(409, "A run is already in progress")
    # Runs synchronously in a background task so the request returns immediately;
    # the frontend polls GET /api/status for progress.
    background_tasks.add_task(execute_run, trigger="manual")
    return {"accepted": True}


@router.get("/runs/{run_id}/pdf")
def get_run_pdf(run_id: int):
    run = get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if not run.pdf_path:
        raise HTTPException(404, "This run has no PDF — render/deliver phase not yet implemented")
    path = Path(run.pdf_path)
    if not path.exists():
        raise HTTPException(404, "PDF file missing on disk")
    return FileResponse(path, media_type="application/pdf", filename=path.name)
