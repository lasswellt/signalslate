"""
SQLite-backed run history and source health, via SQLModel. One file DB — fine at this volume
(one run a day, six source-health rows per run).
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from pipeline.clock import utcnow

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "digest.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """
    WAL lets the scheduler thread read while a manual run writes; without it the two can collide
    with "database is locked" once collection takes minutes instead of milliseconds. busy_timeout
    makes a writer wait for the lock rather than failing instantly.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trigger: str  # "manual" | "scheduled"
    status: str = "running"  # running | success | partial | failed
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None  # short human-readable outcome
    error: Optional[str] = None
    pdf_path: Optional[str] = None  # populated once the render phase exists


class SourceHealth(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id")
    source: str  # "m365_<alias>" | "zoom" | "slack_<label>" | "gmail_<label>"
    status: str  # "ok" | "error"
    detail: Optional[str] = None
    checked_at: datetime = Field(default_factory=utcnow)


class CollectedItem(SQLModel, table=True):
    """
    One item pulled from one source during one run: a mail, an event, a task, a chat/Slack message,
    a Zoom meeting. Stored raw as JSON — normalization is Phase 3's job, and keeping the payload
    verbatim means a synthesis change doesn't require re-collecting.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    source: str = Field(index=True)  # "m365_<alias>" | "zoom" | "slack_<label>" | "gmail_<label>"
    item_type: str  # mail | event | task | chat | message | meeting
    external_id: str  # the source's own id — dedupes overlapping collection windows
    occurred_at: datetime  # the item's own timestamp, naive UTC
    payload: str  # JSON blob as returned by the source


class SourceCursor(SQLModel, table=True):
    """
    Per-source watermark, deliberately NOT keyed to a run: a failed run must not lose the last
    known-good position. Lives here rather than in config.json because config.json is user-editable
    web-UI state and a PUT /config full-replace would clobber it.
    """
    source: str = Field(primary_key=True)
    last_success_at: Optional[datetime] = None
    cursor: Optional[str] = None  # unused on filtered reads; present for a later delta/cursor source
    # Runs since this source last collected cleanly. A permanent sub-resource failure (To Do not
    # licensed, say) would otherwise freeze the watermark forever, re-fetching the full backfill
    # window every day for good. See runner.MAX_STUCK_RUNS.
    consecutive_failures: int = 0


def _add_missing_columns() -> None:
    """
    Add columns that exist in the models but not yet on disk.

    SQLModel.metadata.create_all() creates missing *tables* and silently no-ops on tables that
    already exist — it never issues ALTER TABLE. data/ is bind-mounted and survives redeploys, so
    without this a model gaining a column makes every query against that table raise
    "no such column" on an existing install, and every run fails until someone edits SQLite by hand.

    Deliberately minimal: adds columns, never drops or retypes them. Anything beyond that wants a
    real migration tool.
    """
    with engine.connect() as conn:
        for table_name, table in SQLModel.metadata.tables.items():
            on_disk = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})")}
            if not on_disk:
                continue  # table doesn't exist yet; create_all will make it complete

            for column in table.columns:
                if column.name in on_disk:
                    continue
                ddl = f"{column.name} {column.type.compile(engine.dialect)}"
                # SQLite refuses a NOT NULL column without a default on a populated table.
                default = getattr(column.default, "arg", None)
                if default is not None and not callable(default):
                    ddl += f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {default}"
                elif not column.nullable:
                    continue  # can't add safely; leave it for a real migration
                conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")
                print(f"[signalslate] migrated: added {table_name}.{column.name}")
        conn.commit()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def get_session() -> Session:
    return Session(engine)


def latest_run() -> Optional[Run]:
    with get_session() as session:
        return session.exec(select(Run).order_by(Run.id.desc())).first()


def list_runs(limit: int = 50) -> list[Run]:
    with get_session() as session:
        return list(session.exec(select(Run).order_by(Run.id.desc()).limit(limit)))


def get_run(run_id: int) -> Optional[Run]:
    with get_session() as session:
        return session.get(Run, run_id)


def source_health_for_run(run_id: int) -> list[SourceHealth]:
    with get_session() as session:
        return list(session.exec(select(SourceHealth).where(SourceHealth.run_id == run_id)))


def items_for_run(run_id: int) -> list[CollectedItem]:
    with get_session() as session:
        return list(session.exec(select(CollectedItem).where(CollectedItem.run_id == run_id)))


def item_counts_for_run(run_id: int) -> dict[str, int]:
    """{source: count} for one run — feeds the run summary string without loading every payload."""
    counts: dict[str, int] = {}
    for item in items_for_run(run_id):
        counts[item.source] = counts.get(item.source, 0) + 1
    return counts


def existing_external_ids(source: str, external_ids: list[str]) -> set[str]:
    """
    Which of these ids this source has already stored.

    Scoped to the ids being offered rather than everything ever collected, so the query stays small
    as history grows. Collection windows deliberately overlap, so this runs on every insert.
    """
    if not external_ids:
        return set()
    with get_session() as session:
        rows = session.exec(
            select(CollectedItem.external_id)
            .where(CollectedItem.source == source)
            .where(CollectedItem.external_id.in_(external_ids))
        ).all()
    return set(rows)


def get_cursor(source: str) -> Optional[SourceCursor]:
    with get_session() as session:
        return session.get(SourceCursor, source)


def set_cursor(source: str, last_success_at: datetime, cursor: Optional[str] = None) -> None:
    """Advance the watermark and clear the failure streak. Upserts on first collection."""
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is None:
            row = SourceCursor(source=source)
        row.last_success_at = last_success_at
        row.cursor = cursor
        row.consecutive_failures = 0
        session.add(row)
        session.commit()


def record_failure(source: str) -> int:
    """Count one non-clean collection for this source. Returns the new streak length."""
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is None:
            row = SourceCursor(source=source)
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        session.add(row)
        session.commit()
        return row.consecutive_failures


def reap_orphaned_runs() -> int:
    """
    Mark any run still flagged "running" at startup as failed. Returns how many.

    execute_run's finally block covers exceptions inside the process, but not a container restart,
    an OOM kill, or a compose down mid-collection. Without this, one such interruption strands a
    row at "running" forever — and since that state hard-blocks every future run (409 from the API,
    a swallowed tick in the scheduler), the digest would stop silently and need hand-edited SQLite
    to recover.
    """
    with get_session() as session:
        stranded = list(session.exec(select(Run).where(Run.status == "running")))
        for run in stranded:
            run.status = "failed"
            run.error = "Interrupted — the process stopped before this run finished."
            run.finished_at = run.finished_at or utcnow()
            session.add(run)
        session.commit()
    return len(stranded)


def has_running_run() -> bool:
    """True if a run is already in flight — the guard against the scheduler and a manual trigger overlapping."""
    with get_session() as session:
        return session.exec(select(Run).where(Run.status == "running")).first() is not None


def latest_source_health() -> list[SourceHealth]:
    """One row per source: the most recent health check recorded for it, across all runs."""
    with get_session() as session:
        rows = session.exec(select(SourceHealth).order_by(SourceHealth.checked_at.desc())).all()
    seen: dict[str, SourceHealth] = {}
    for row in rows:
        seen.setdefault(row.source, row)
    return list(seen.values())
