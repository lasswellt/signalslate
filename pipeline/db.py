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
    source: str  # "m365_<alias>" | "zoom" | "slack_<label>"
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
    source: str = Field(index=True)  # "m365_<alias>" | "zoom" | "slack_<label>"
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


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)


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


def get_cursor(source: str) -> Optional[SourceCursor]:
    with get_session() as session:
        return session.get(SourceCursor, source)


def set_cursor(source: str, last_success_at: datetime, cursor: Optional[str] = None) -> None:
    """Upsert — the row is created on a source's first successful collection."""
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is None:
            row = SourceCursor(source=source)
        row.last_success_at = last_success_at
        row.cursor = cursor
        session.add(row)
        session.commit()


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
