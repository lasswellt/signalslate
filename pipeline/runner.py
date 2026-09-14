"""
execute_run() is the one function both the scheduler and the manual-trigger API call.

Today it checks every active source's auth health and records it. Phase 2 adds collection between
the check_all_configured() call and the finally block below, writing CollectedItem rows and
advancing each source's SourceCursor. The Run/SourceHealth records, the API contract, and the web
UI don't change when it lands — a run just starts producing items, a richer summary, and eventually
a pdf_path.

Every source is isolated: one source's failure produces an error row, never a dead run. The
terminal status is written in a finally block, because a Run left at status="running" is invisible
to the dashboard's poll loop and never resolves.
"""
from typing import Optional

from pipeline.clock import utcnow
from pipeline.config_store import load_config
from pipeline.db import Run, SourceHealth, get_session, has_running_run, item_counts_for_run
from pipeline.health import HealthResult, check_all_configured


class RunAlreadyInProgress(RuntimeError):
    """Raised when a run is started while another is still in flight."""


def _summarize(results: list[HealthResult], counts: dict[str, int]) -> tuple[str, str, Optional[str]]:
    """(status, summary, error) from the per-source results plus whatever was collected."""
    ok_count = sum(1 for r in results if r.status == "ok")
    error_count = len(results) - ok_count

    if not results:
        return "success", "No sources active in config — nothing checked.", None

    collected = sum(counts.values())
    detail = f" Collected {collected} items." if collected else ""

    if error_count == 0:
        return "success", f"All {ok_count} sources healthy.{detail}", None
    if ok_count == 0:
        return "failed", f"All {error_count} sources failed.{detail}", "All configured sources failed their health check."
    return "partial", f"{ok_count} ok, {error_count} failing — see per-source detail.{detail}", None


def execute_run(trigger: str = "manual") -> Run:
    # The scheduler thread and a manual trigger can both land here; without this guard they
    # interleave writes and produce two half-finished runs for the same window.
    if has_running_run():
        raise RunAlreadyInProgress("A run is already in progress")

    config = load_config()

    with get_session() as session:
        run = Run(trigger=trigger, status="running")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    results: list[HealthResult] = []
    fatal: Optional[str] = None

    try:
        # check_all_configured already isolates requests errors per source; this catches anything
        # it doesn't (a corrupt MSAL cache, a missing tokens/ dir) so the run still terminates.
        results = check_all_configured(config["active_sources"])
    except Exception as exc:  # noqa: BLE001 — a dead run is worse than a broad except
        fatal = f"{type(exc).__name__}: {exc}"
    finally:
        counts = item_counts_for_run(run_id)
        status, summary, error = _summarize(results, counts)
        if fatal is not None:
            status, error = "failed", fatal

        with get_session() as session:
            run = session.get(Run, run_id)
            for r in results:
                session.add(SourceHealth(run_id=run_id, source=r.source, status=r.status, detail=r.detail))

            run.finished_at = utcnow()
            run.status = status
            run.summary = summary
            run.error = error

            session.add(run)
            session.commit()
            session.refresh(run)

    return run
