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
from pipeline.collectors import DEFAULT_LOOKBACK, MAX_BACKFILL, OVERLAP, CollectionResult, dispatch
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

    Normally 24h back. If the last success was longer ago — a failed run, a box that was off —
    start from that watermark instead so the missed window is backfilled rather than silently
    skipped. The overlap covers items that land slightly out of order; dedupe on external_id makes
    re-reading them free.

    Bounded by MAX_BACKFILL. An unbounded window looks reasonable until a machine comes back after
    a month off and the first run asks every API for a month of history.
    """
    default_start = until - DEFAULT_LOOKBACK
    floor = until - MAX_BACKFILL

    cursor = get_cursor(source)
    if cursor is None or cursor.last_success_at is None:
        return default_start

    return max(floor, min(default_start, cursor.last_success_at - OVERLAP))


def _collect_source(source: str, until: datetime) -> CollectionResult:
    """One source, over the window its cursor says is outstanding."""
    return dispatch(source, collection_window(source, until), until)


def _persist(run_id: int, result: CollectionResult) -> int:
    """
    Store this source's new items. Returns how many were actually new.

    Two dedupes, because there are two ways a duplicate arrives: the same item seen in an earlier
    run (collection windows overlap deliberately), and the same item twice within one result — a
    page boundary that shifts mid-pagination, or a message reachable through two paths. The
    check-then-insert is not atomic, which is safe only because the overlap guard in execute_run
    means two runs never collect concurrently. If that guard is ever relaxed, this needs a unique
    constraint on (source, external_id) instead.
    """
    if not result.items:
        return 0

    seen: set[str] = set()
    unique = []
    for item in result.items:
        if item.external_id in seen:
            continue
        seen.add(item.external_id)
        unique.append(item)

    known = existing_external_ids(result.source, [i.external_id for i in unique])
    fresh = [i for i in unique if i.external_id not in known]
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
    failed_sources: set[str],
    partial_sources: set[str],
) -> tuple[str, str, Optional[str]]:
    """
    (status, summary, error) from the health checks and what collection actually produced.

    Three tiers, because health alone isn't enough: judging on it would report "partial" for a run
    where every source authenticated perfectly and then collected nothing, which reads as a minor
    problem and isn't one. But a source that collected *some* of its window is not a hard failure
    either — so a lone partially-collecting source gives a partial run, not a failed one.
    """
    if not health:
        return "success", "No sources active in config — nothing checked.", None

    checked = {r.source for r in health}
    unhealthy = {r.source for r in health if r.status != "ok"}
    bad = (unhealthy | failed_sources) - partial_sources
    partial = partial_sources - unhealthy
    good = checked - bad - partial

    total = sum(collected.values())
    breakdown = ", ".join(f"{s}={n}" for s, n in sorted(collected.items()) if n) or "nothing new"
    error = "; ".join(failures) or None

    if not bad and not partial:
        return "success", f"All {len(good)} sources healthy. Collected {total} items ({breakdown}).", None
    if not good and not partial:
        return (
            "failed",
            f"All {len(bad)} sources failed. Collected {total} items ({breakdown}).",
            error or "All configured sources failed their health check.",
        )
    tiers = f"{len(good)} ok"
    if partial:
        tiers += f", {len(partial)} partial"
    tiers += f", {len(bad)} failing"
    return "partial", f"{tiers}. Collected {total} items ({breakdown}).", error


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
    failed_sources: set[str] = set()
    partial_sources: set[str] = set()
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
                failed_sources.add(source)
                continue

            # Items are kept even when the source errored: a run that got half of Slack before
            # hitting the rate-limit canary should keep that half. Safe because the cursor below
            # does not advance, so the window is re-read next time and the dedupe absorbs it.
            collected[source] = _persist(run_id, result)
            if result.status == "ok":
                # Only a clean collection advances the watermark. A partial one must re-read its
                # window next time — otherwise a catch-up run that half-failed moves the cursor to
                # now and the rest of the backfill gap is lost for good.
                set_cursor(source, until)
            else:
                failures.append(f"{source}: {result.detail}")
                failed_sources.add(source)
                if result.status == "partial":
                    partial_sources.add(source)
    except Exception as exc:  # noqa: BLE001 — a dead run is worse than a broad except
        fatal = f"{type(exc).__name__}: {exc}"
    finally:
        status, summary, error = _summarize(health, collected, failures, failed_sources, partial_sources)
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
