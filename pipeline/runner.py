"""
execute_run() is the one function both the scheduler and the manual-trigger API call.

Today it does the full, real thing this pipeline can currently do: check every active source's
auth health and record it. Phases 2-6 (collect -> synthesize -> render -> deliver) extend this
same function with real work; the Run/SourceHealth records, the API contract, and the web UI
don't need to change when that lands — a run just starts producing a pdf_path and a richer
summary.
"""
from datetime import datetime

from pipeline.config_store import load_config
from pipeline.db import Run, SourceHealth, get_session
from pipeline.health import check_all_configured


def execute_run(trigger: str = "manual") -> Run:
    config = load_config()

    with get_session() as session:
        run = Run(trigger=trigger, status="running")
        session.add(run)
        session.commit()
        session.refresh(run)

    results = check_all_configured(config["active_sources"])

    ok_count = sum(1 for r in results if r.status == "ok")
    error_count = len(results) - ok_count

    with get_session() as session:
        run = session.get(Run, run.id)
        for r in results:
            session.add(SourceHealth(run_id=run.id, source=r.source, status=r.status, detail=r.detail))

        run.finished_at = datetime.utcnow()
        if not results:
            run.status = "success"
            run.summary = "No sources active in config — nothing checked."
        elif error_count == 0:
            run.status = "success"
            run.summary = f"All {ok_count} sources healthy. Collect/synthesize/render/deliver not yet implemented (Phase 2+)."
        elif ok_count == 0:
            run.status = "failed"
            run.error = "All configured sources failed their health check."
        else:
            run.status = "partial"
            run.summary = f"{ok_count} ok, {error_count} failing — see per-source detail."

        session.add(run)
        session.commit()
        session.refresh(run)
        return run
