"""
Collector routes: per-source state, run one source, dry run, watermark reset, failure streak reset,
and the items a source has stored (docs/_research/2026-09-21_management-ui.md section 3).

Design decisions:

- Every route validates the source id against health.known_sources() first and answers 404
  unknown_source otherwise. The run route deliberately does NOT require the source to be toggled on:
  execute_run(only=...) collects a switched-off source on purpose, since the operator asked for
  exactly that one.
- Dry runs are slow network calls, so POST returns a job id at once and the work runs in a daemon
  thread. The job table is process-local, capped at _MAX_JOBS and purged on every access: a
  finished job is dropped _JOB_TTL after it finished, a job that never finished _JOB_TTL after it
  started (a hung collector must not hold a slot forever). When the cap is reached the oldest
  FINISHED job is evicted; if every slot is still running the request gets 429, because evicting a
  running job would turn its client's next poll into a 404 for work that is still happening.
- pipeline.collect.dry_run returns naive UTC datetimes. They are turned into Z strings, and every
  string is redacted, before the result is stored, so the table never holds an unredacted copy and
  the response model can only ever be JSON. A dry_run that raises marks the job failed with no
  result: the exception text may quote a response body that carries a credential.
- The list of items carries a one-line preview only. The full payload is one request at a time
  (GET .../items/{id}), redacted first and cut to _MAX_PAYLOAD_BYTES afterwards: cutting first could
  leave half of a token that redact() no longer recognises.
- The item detail route checks that the row belongs to the source in the URL, so an id from
  another source is a 404 and never leaks that source's payload.
- Handlers are plain `def` (threadpool): they read SQLite and the connection store synchronously.
"""
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.serialize import iso_z
from pipeline import collect, config_store, connections, db, health, runner
from pipeline.clock import utcnow
from pipeline.redact import redact

router = APIRouter(tags=["collectors"])

_log = logging.getLogger(__name__)

# Same cap the runner applies to text it persists: applied after redaction, never before.
_MAX_TEXT = 1000
_MAX_PAYLOAD_BYTES = 20 * 1024
_DEFAULT_PAGE = 50

_MAX_JOBS = 20
_JOB_TTL = timedelta(minutes=15)

RESET_NOTE = (
    "The next run collects from the watermark minus a 30 minute overlap, but never further back than "
    "7 days. A watermark newer than about 24 hours behaves like no watermark: the run collects the "
    "last 24 hours."
)


class LastAttempt(BaseModel):
    at: str
    status: Optional[str]
    detail: Optional[str]
    item_count: Optional[int]


class CollectorState(BaseModel):
    source: str
    active: bool
    watermark: Optional[str]
    consecutive_failures: int
    stuck_threshold: int
    last_attempt: Optional[LastAttempt]
    item_count: int


class Accepted(BaseModel):
    accepted: bool


class DryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: int = Field(default=24, ge=1, le=collect.MAX_HOURS)
    limit: int = Field(default=5, ge=0, le=collect.MAX_LIMIT)


class DryRunAccepted(BaseModel):
    job_id: str


class DryRunWindow(BaseModel):
    since: str
    until: str
    hours: int


class DryRunItem(BaseModel):
    item_type: str
    occurred_at: str
    external_id: str
    preview: str


class DryRunResult(BaseModel):
    source: str
    status: str
    detail: str
    window: DryRunWindow
    count: int
    by_type: dict[str, int]
    items: list[DryRunItem]
    duration_ms: int


class DryRunJob(BaseModel):
    status: str
    result: Optional[DryRunResult] = None


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days_back: Optional[int] = Field(default=None, ge=1, le=7)


class ResetOut(BaseModel):
    source: str
    watermark: Optional[str]
    note: str


class ClearFailuresOut(BaseModel):
    source: str
    consecutive_failures: int


class ItemSummary(BaseModel):
    id: int
    item_type: str
    external_id: str
    occurred_at: str
    preview: str


class ItemPage(BaseModel):
    items: list[ItemSummary]
    next_before_id: Optional[int]
    total: int


class ItemDetail(BaseModel):
    id: int
    item_type: str
    external_id: str
    occurred_at: str
    payload: str
    truncated: bool


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _require_source(source: str) -> None:
    if source not in health.known_sources():
        raise _coded(404, "unknown_source", "Unknown source")


def _z(dt: datetime) -> str:
    """iso_z for a value that is never None."""
    return iso_z(dt) or ""


def _scrub(text: Optional[str], known: list[str]) -> Optional[str]:
    if text is None:
        return None
    return redact(text, known, max_len=_MAX_TEXT)


def _state(source: str, active: dict[str, bool], known: list[str]) -> CollectorState:
    cursor = db.get_cursor(source)
    last_attempt = None
    if cursor is not None and cursor.last_attempt_at is not None:
        last_attempt = LastAttempt(
            at=_z(cursor.last_attempt_at),
            status=cursor.last_status,
            detail=_scrub(cursor.last_detail, known),
            item_count=cursor.last_item_count,
        )
    return CollectorState(
        source=source,
        active=active.get(source, False),
        watermark=iso_z(cursor.last_success_at) if cursor is not None else None,
        consecutive_failures=(cursor.consecutive_failures or 0) if cursor is not None else 0,
        stuck_threshold=runner.MAX_STUCK_RUNS,
        last_attempt=last_attempt,
        item_count=db.count_items(source),
    )


@router.get("/collectors", response_model=list[CollectorState])
def list_collectors() -> list[CollectorState]:
    """Every declared source with its toggle, watermark, failure streak, last attempt and item count."""
    active = config_store.load_config()["active_sources"]
    known = connections.secret_values()
    return [_state(source, active, known) for source in health.known_sources()]


# --- dry-run jobs -------------------------------------------------------------------------------


@dataclass
class _Job:
    created_at: datetime
    status: str = "running"
    finished_at: Optional[datetime] = None
    result: Optional[DryRunResult] = None


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()


def _purge_locked(now: datetime) -> None:
    expired = [job_id for job_id, job in _jobs.items() if (job.finished_at or job.created_at) + _JOB_TTL <= now]
    for job_id in expired:
        del _jobs[job_id]


def _register_job(now: datetime) -> str:
    """Adds a running job and returns its id; raises 429 when every slot is still running."""
    with _jobs_lock:
        _purge_locked(now)
        if len(_jobs) >= _MAX_JOBS:
            oldest_finished = next((job_id for job_id, job in _jobs.items() if job.finished_at is not None), None)
            if oldest_finished is None:
                raise _coded(429, "too_many_dry_runs", "Too many dry runs are in progress; try again shortly")
            del _jobs[oldest_finished]
        job_id = uuid.uuid4().hex
        _jobs[job_id] = _Job(created_at=now)
        return job_id


def _finish_job(job_id: str, status: str, result: Optional[DryRunResult]) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:  # purged while running: nobody can ask for it any more
            return
        job.status = status
        job.result = result
        job.finished_at = utcnow()


def _wire(value: Any, known: list[str]) -> Any:
    """dry_run()'s dict with datetimes as Z strings and every string redacted."""
    if isinstance(value, datetime):
        return iso_z(value)
    if isinstance(value, str):
        return redact(value, known, max_len=_MAX_TEXT)
    if isinstance(value, dict):
        return {key: _wire(item, known) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item, known) for item in value]
    return value


def _dry_run_worker(job_id: str, source: str, hours: int, limit: int) -> None:
    try:
        # One frozen environment for both the source check inside dry_run() and the collector call.
        with health.env_snapshot():
            data = collect.dry_run(source, hours=hours, limit=limit)
        result = DryRunResult.model_validate(_wire(data, connections.secret_values()))
    except Exception as exc:  # noqa: BLE001 — a job never raises; its exception text may carry a credential
        _log.warning("dry run for %s failed with %s", source, type(exc).__name__)
        _finish_job(job_id, "failed", None)
        return
    _finish_job(job_id, "done", result)


@router.post("/collectors/{source}/dry-run", status_code=202, response_model=DryRunAccepted)
def start_dry_run(source: str, body: DryRunBody) -> DryRunAccepted:
    """
    Starts a read-only collection preview and returns its job id.

    Raises 404 unknown_source, 429 too_many_dry_runs (the job table is full of running jobs).
    """
    _require_source(source)
    job_id = _register_job(utcnow())
    threading.Thread(
        target=_dry_run_worker, args=(job_id, source, body.hours, body.limit), daemon=True, name="dry-run"
    ).start()
    return DryRunAccepted(job_id=job_id)


# Registered before /collectors/{source}/items: both are GET with three segments.
@router.get("/collectors/dry-run/{job_id}", response_model=DryRunJob)
def get_dry_run(job_id: str) -> DryRunJob:
    """
    The job's status and, once done, its redacted result. A failed job has no result.

    Raises 404 dry_run_not_found for an id that never existed or has been purged.
    """
    with _jobs_lock:
        _purge_locked(utcnow())
        job = _jobs.get(job_id)
        if job is None:
            raise _coded(404, "dry_run_not_found", "Dry run not found or expired")
        return DryRunJob(status=job.status, result=job.result)


# --- run, reset ---------------------------------------------------------------------------------


@router.post("/collectors/{source}/run", status_code=202, response_model=Accepted)
def run_source(source: str, background_tasks: BackgroundTasks) -> Accepted:
    """
    Runs one source now, whether or not it is toggled on.

    Raises 404 unknown_source, 409 run_in_progress. The in-flight check is repeated here because an
    exception in a background task is swallowed and the caller would be told a run was accepted.
    """
    _require_source(source)
    if db.has_running_run():
        raise _coded(409, "run_in_progress", "A run is already in progress")
    background_tasks.add_task(runner.execute_run, trigger="manual", only=source)
    return Accepted(accepted=True)


@router.post("/collectors/{source}/reset", response_model=ResetOut)
def reset_watermark(source: str, body: Optional[ResetBody] = None) -> ResetOut:
    """
    Moves the watermark back `days_back` days, or deletes it when that is null or the body is absent,
    which also clears the failure streak. See RESET_NOTE for what the runner then does with it.

    Raises 404 unknown_source, 422 for days_back outside 1..7.
    """
    _require_source(source)
    days_back = body.days_back if body is not None else None
    target = utcnow() - timedelta(days=days_back) if days_back is not None else None
    db.reset_cursor(source, target)
    return ResetOut(source=source, watermark=iso_z(target), note=RESET_NOTE)


@router.post("/collectors/{source}/clear-failures", response_model=ClearFailuresOut)
def clear_failures(source: str) -> ClearFailuresOut:
    """Zeroes the failure streak and leaves the watermark alone. Raises 404 unknown_source."""
    _require_source(source)
    db.clear_failures(source)
    return ClearFailuresOut(source=source, consecutive_failures=0)


# --- items --------------------------------------------------------------------------------------


def _item_id(row: db.CollectedItem) -> int:
    if row.id is None:
        raise RuntimeError("stored item without an id")
    return row.id


def _preview(payload_text: str, known: list[str]) -> str:
    try:
        parsed = json.loads(payload_text)
    except (ValueError, RecursionError):
        parsed = None
    text = collect.preview(parsed if isinstance(parsed, dict) else {})
    return redact(text, known, max_len=_MAX_TEXT)


@router.get("/collectors/{source}/items", response_model=ItemPage)
def list_source_items(
    source: str,
    limit: int = Query(default=_DEFAULT_PAGE, ge=1, le=db.MAX_ITEM_PAGE),
    before_id: Optional[int] = Query(default=None, ge=1),
    item_type: Optional[str] = Query(default=None, max_length=50),
) -> ItemPage:
    """
    One keyset page of a source's items, newest first, with a one-line preview each. Pass
    next_before_id back as before_id for the next page; it is null on the last page. `total` counts
    the whole source (narrowed by item_type when given), not the page.

    Raises 404 unknown_source.
    """
    _require_source(source)
    rows = db.list_items(source, item_type=item_type, before_id=before_id, limit=limit)
    known = connections.secret_values()
    items = [
        ItemSummary(
            id=_item_id(row),
            item_type=row.item_type,
            external_id=row.external_id,
            occurred_at=_z(row.occurred_at),
            preview=_preview(row.payload, known),
        )
        for row in rows
    ]
    next_before_id = None
    if items and len(rows) == limit:
        last_id = items[-1].id
        # Probing beats guessing from len(rows) == limit, which would send the reader to an empty last page.
        if db.list_items(source, item_type=item_type, before_id=last_id, limit=1):
            next_before_id = last_id
    return ItemPage(items=items, next_before_id=next_before_id, total=db.count_items(source, item_type))


@router.get("/collectors/{source}/items/{item_id}", response_model=ItemDetail)
def get_source_item(source: str, item_id: int) -> ItemDetail:
    """
    One item with its full payload as redacted, pretty-printed JSON text, cut to 20 KB (`truncated`
    says so). Render it as text, never as HTML.

    Raises 404 unknown_source, 404 item_not_found (absent, or stored under another source).
    """
    _require_source(source)
    with db.get_session() as session:
        row = session.get(db.CollectedItem, item_id)
    if row is None or row.source != source:
        raise _coded(404, "item_not_found", "Item not found")

    try:
        text = json.dumps(json.loads(row.payload), indent=2, ensure_ascii=False)
    except (ValueError, RecursionError):
        text = row.payload
    encoded = redact(text, connections.secret_values()).encode("utf-8")
    truncated = len(encoded) > _MAX_PAYLOAD_BYTES
    payload = encoded[:_MAX_PAYLOAD_BYTES].decode("utf-8", errors="ignore") if truncated else encoded.decode("utf-8")
    return ItemDetail(
        id=_item_id(row),
        item_type=row.item_type,
        external_id=row.external_id,
        occurred_at=_z(row.occurred_at),
        payload=payload,
        truncated=truncated,
    )
