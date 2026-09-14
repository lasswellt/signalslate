"""
execute_run() is the one function both the scheduler and the manual-trigger API call.

It checks every active source's auth health, then collects the last 24h from each one. Phases 3-5
(synthesize -> render -> deliver) extend the same function; the Run/SourceHealth records, the API
contract, and the web UI don't change when they land — a run just starts producing a digest and a
pdf_path alongside the items it already stores.

Every source is isolated: one source's failure produces an error row, never a dead run. The
terminal status is written in a finally block, because a Run left at status="running" is invisible
to the dashboard's poll loop and never resolves.
"""
import json
from datetime import datetime
from typing import Optional

from pipeline.clock import utcnow
from pipeline.collectors import DEFAULT_LOOKBACK, OVERLAP, CollectionResult, dispatch
from pipeline.config_store import load_config
from pipeline.db import (
    CollectedItem,
    Run,
    SourceHealth,
    existing_external_ids,
    get_cursor,
    get_session,
    has_running_run,
    set_cursor,
)
from pipeline.health import HealthResult, check_all_configured, m365_aliases, slack_workspaces


class RunAlreadyInProgress(RuntimeError):
    """Raised when a run is started while another is still in flight."""


def collection_window(source: str, until: datetime) -> datetime:
    """
    Where to start collecting for this source.

    Normally 24h back. But if the last successful run was longer ago than that — a failed run, a
    box that was off — start from that watermark instead so the missed window is backfilled rather
    than silently skipped. The overlap covers items that land slightly out of order; dedupe on
    external_id makes re-reading them free.
    """
    default_start = until - DEFAULT_LOOKBACK
    cursor = get_cursor(source)
    if cursor is None or cursor.last_success_at is None:
        return default_start
    return min(default_start, cursor.last_success_at - OVERLAP)


def _collect_source(source: str, until: datetime) -> CollectionResult:
    """One source, over the window its cursor says is outstanding."""
    return dispatch(source, collection_window(source, until), until)


def _persist(run_id: int, result: CollectionResult) -> int:
    """Store this source's new items. Returns how many were actually new."""
    if not result.items:
        return 0

    known = existing_external_ids(result.source, [i.external_id for i in result.items])
    fresh = [i for i in result.items if i.external_id not in known]
    if not fresh:
        return 0

    with get_session() as session:
        for item in fresh:
            session.add(
                CollectedItem(
                    run_id=run_id,
                    source=result.source,
                    item_type=item.item_type,
                    external_id=item.external_id,
                    occurred_at=item.occurred_at,
                    payload=json.dumps(item.payload, default=str),
                )
            )
        session.commit()
    return len(fresh)


def _active(config: dict) -> list[str]:
    """Declared sources that are toggled on, in a stable order."""
    declared = [f"m365_{a}" for a in m365_aliases()] + ["zoom"] + [f"slack_{l}" for l in slack_workspaces()]
    return [s for s in declared if config["active_sources"].get(s, False)]


def _summarize(
    health: list[HealthResult],
    collected: dict[str, int],
    failures: list[str],
) -> tuple[str, str, Optional[str]]:
    """(status, summary, error) from the health checks plus what collection actually produced."""
    if not health:
        return "success", "No sources active in config — nothing checked.", None

    ok_count = sum(1 for r in health if r.status == "ok")
    error_count = len(health) - ok_count
    total = sum(collected.values())
    breakdown = ", ".join(f"{s}={n}" for s, n in sorted(collected.items()) if n) or "nothing new"

    if error_count == 0 and not failures:
        return "success", f"All {ok_count} sources healthy. Collected {total} items ({breakdown}).", None
    if ok_count == 0:
        return (
            "failed",
            f"All {error_count} sources failed.",
            "; ".join(failures) or "All configured sources failed their health check.",
        )
    detail = f"{ok_count} ok, {error_count} failing. Collected {total} items ({breakdown})."
    return "partial", detail, "; ".join(failures) or None


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

    until = utcnow()
    health: list[HealthResult] = []
    collected: dict[str, int] = {}
    failures: list[str] = []
    fatal: Optional[str] = None

    try:
        health = check_all_configured(config["active_sources"])
        healthy = {r.source for r in health if r.status == "ok"}

        for source in _active(config):
            # No point collecting through a token the health check just rejected — it would fail
            # slowly and report the same thing twice.
            if source not in healthy:
                continue
            try:
                result = _collect_source(source, until)
            except Exception as exc:  # noqa: BLE001 — one source must never end the run
                failures.append(f"{source}: {type(exc).__name__}: {exc}")
                continue

            collected[source] = _persist(run_id, result)
            if result.status == "error":
                failures.append(f"{source}: {result.detail}")
            else:
                # Only advance the watermark on a clean-enough collection, so a partial failure
                # re-reads its window next time instead of stepping over the gap.
                set_cursor(source, until)
    except Exception as exc:  # noqa: BLE001 — a dead run is worse than a broad except
        fatal = f"{type(exc).__name__}: {exc}"
    finally:
        status, summary, error = _summarize(health, collected, failures)
        if fatal is not None:
            status, error = "failed", fatal

        with get_session() as session:
            run = session.get(Run, run_id)
            for r in health:
                session.add(SourceHealth(run_id=run_id, source=r.source, status=r.status, detail=r.detail))

            run.finished_at = utcnow()
            run.status = status
            run.summary = summary
            run.error = error

            session.add(run)
            session.commit()
            session.refresh(run)

    return run
