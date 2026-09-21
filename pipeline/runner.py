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
import threading
from datetime import datetime, timedelta
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
    record_attempt,
    record_failure,
    set_cursor,
)
from pipeline.health import HealthResult, check_all_configured, env_snapshot, known_sources


# How many consecutive non-clean collections before a source's watermark advances anyway.
# Holding it back forever assumes failures are transient; a permanently 403-ing sub-resource (To Do
# not licensed) would otherwise pin the window at MAX_BACKFILL and re-fetch a week of Graph every
# day, for good — and if that window ever exceeds the pagination cap, the source never recovers.
MAX_STUCK_RUNS = 3


# Serialises "is a run in flight?" with "insert my Run row". Without it the scheduler and a UI
# trigger can both read False from has_running_run() and both insert, which is the exact overlap the
# guard exists to prevent. Process-local, which is enough: one uvicorn worker owns the scheduler and
# the API; a second process would need a partial unique index on Run(status='running') instead.
_start_lock = threading.Lock()


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


def backfill_shortfall(source: str, until: datetime) -> Optional[timedelta]:
    """
    How much history the MAX_BACKFILL cap is about to skip, or None when nothing is skipped.

    After a long outage the window floors at MAX_BACKFILL and the run then advances the watermark
    past everything older. That data is unreachable from then on, so the run must say so rather
    than report a clean success over a hole.

    Returns a timedelta, not a day count: whole days truncate a 20-hour gap to 0, which a caller
    testing truthiness then treats as "nothing skipped" — the exact silence this exists to break.
    Measured against the real window start, which includes OVERLAP.
    """
    cursor = get_cursor(source)
    if cursor is None or cursor.last_success_at is None:
        return None
    skipped = collection_window(source, until) - (cursor.last_success_at - OVERLAP)
    return skipped if skipped.total_seconds() > 0 else None


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
    """
    Declared sources that are toggled on, in a stable order.

    Delegates to known_sources() rather than keeping its own list: a hand-copied list here once let
    a newly declared source pass its health check yet never be collected, with no error anywhere.
    """
    return [s for s in known_sources() if config["active_sources"].get(s, False)]


def _summarize(
    health: list[HealthResult],
    collected: dict[str, int],
    failures: list[str],
    failed_sources: set[str],
    partial_sources: set[str],
    notes: Optional[list[str]] = None,
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

    # Derived in strict precedence — unhealthy beats partial beats good — so the three tiers are
    # disjoint by construction. The earlier form could put a source that was both unhealthy and
    # partial into "good"; unreachable today only because collection is skipped for unhealthy
    # sources, which is too fragile a reason to rely on.
    checked = {r.source for r in health}
    unhealthy = {r.source for r in health if r.status != "ok"}
    bad = unhealthy | (failed_sources - partial_sources)
    partial = (partial_sources | failed_sources) - bad
    good = checked - bad - partial

    total = sum(collected.values())
    breakdown = ", ".join(f"{s}={n}" for s, n in sorted(collected.items()) if n) or "nothing new"
    error = "; ".join(failures) or None

    # Caveats ride on the summary, not the error: a capped backfill is not a failure, but a run
    # that quietly skipped three weeks of history must not report an unqualified success.
    caveat = (" " + " ".join(notes)) if notes else ""

    if not bad and not partial:
        return (
            "success",
            f"All {len(good)} sources healthy. Collected {total} items ({breakdown}).{caveat}",
            None,
        )
    if not good and not partial:
        return (
            "failed",
            f"All {len(bad)} sources failed. Collected {total} items ({breakdown}).{caveat}",
            error or "All configured sources failed their health check.",
        )
    tiers = f"{len(good)} ok"
    if partial:
        tiers += f", {len(partial)} partial"
    tiers += f", {len(bad)} failing"
    return "partial", f"{tiers}. Collected {total} items ({breakdown}).{caveat}", error


def execute_run(trigger: str = "manual", only: Optional[str] = None) -> Run:
    """
    Check and collect every active source, or just `only`.

    A single-source run is a full run with the source set narrowed: same Run and SourceHealth rows,
    same per-source cursor, backfill and failure-streak logic. Its Run.trigger is "manual-source"
    whatever `trigger` says, so the history can tell it from a full run; pipeline/db.py's comment on
    Run.trigger predates the value and is not updated here. It also runs a source that is toggled off,
    since the operator asked for exactly that one.

    The whole run executes inside one env_snapshot(): the health check, known_sources() and the
    collector must agree on the configuration even if a connection is edited while it runs.

    Raises ValueError (before any Run row exists) when `only` is not a declared source, and
    RunAlreadyInProgress when another run is in flight.
    """
    with env_snapshot():
        return _execute_run(trigger, only)


def _execute_run(trigger: str, only: Optional[str]) -> Run:
    if only is not None:
        if only not in known_sources():
            raise ValueError(f"Unknown source: {only!r}")
        trigger = "manual-source"

    config = load_config()
    if only is not None:
        config = {**config, "active_sources": {only: True}}

    # The scheduler thread and a manual trigger can both land here; without this guard they
    # interleave writes and produce two half-finished runs for the same window.
    with _start_lock:
        if has_running_run():
            raise RunAlreadyInProgress("A run is already in progress")

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
    notes: list[str] = []
    fatal: Optional[str] = None

    try:
        health = check_all_configured(config["active_sources"])
        healthy = {r.source for r in health if r.status == "ok"}

        for source in _active(config):
            # No point collecting through a token the health check just rejected — it would fail
            # slowly and report the same thing twice.
            if source not in healthy:
                continue
            shortfall = backfill_shortfall(source, until)
            if shortfall is not None:
                hours = round(shortfall.total_seconds() / 3600)
                notes.append(f"{source}: backfill capped — {hours}h before the window were skipped.")

            try:
                result = _collect_source(source, until)
            except Exception as exc:  # noqa: BLE001 — one source must never end the run
                # Class name only: the message can carry an upstream body or a credential.
                record_attempt(source, "error", type(exc).__name__, None)
                failures.append(f"{source}: {type(exc).__name__}: {exc}")
                failed_sources.add(source)
                # Counts toward the streak exactly as a returned failure does. A collector that
                # *raises* every run (a corrupt MSAL cache makes deserialize throw, which
                # collect_m365 doesn't catch) is the strongest case for eventually advancing —
                # missing it here left the watermark frozen forever in the worst scenario.
                if record_failure(source) >= MAX_STUCK_RUNS:
                    set_cursor(source, until)
                    failures.append(f"{source}: advancing watermark after repeated hard failures")
                continue

            # Items are kept even when the source errored: a run that got half of Slack before
            # hitting the rate-limit canary should keep that half. Safe because the cursor below
            # does not advance, so the window is re-read next time and the dedupe absorbs it.
            collected[source] = _persist(run_id, result)
            record_attempt(source, result.status, result.detail, collected[source])
            if result.status == "ok":
                # A clean collection advances the watermark and clears any failure streak.
                set_cursor(source, until)
            else:
                failures.append(f"{source}: {result.detail}")
                failed_sources.add(source)
                if result.status == "partial":
                    partial_sources.add(source)

                # Normally a non-clean collection holds the watermark back so the window is
                # re-read. But if a source has been failing this way for MAX_STUCK_RUNS, the
                # failure is not transient and holding the line just re-fetches an ever-larger
                # window forever. Advance, and say so.
                streak = record_failure(source)
                if streak >= MAX_STUCK_RUNS:
                    set_cursor(source, until)
                    failures.append(
                        f"{source}: advancing watermark after {streak} failed runs — "
                        f"any data still missing from that window will not be retried"
                    )
    except Exception as exc:  # noqa: BLE001 — a dead run is worse than a broad except
        fatal = f"{type(exc).__name__}: {exc}"
    finally:
        status, summary, error = _summarize(
            health, collected, failures, failed_sources, partial_sources, notes
        )
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
