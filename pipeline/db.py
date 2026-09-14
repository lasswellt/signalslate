"""
SQLite-backed run history and source health, via SQLModel. One file DB — fine at this volume
(one run a day, six source-health rows per run).
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "digest.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trigger: str  # "manual" | "scheduled"
    status: str = "running"  # running | success | partial | failed
    started_at: datetime = Field(default_factory=datetime.utcnow)
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
    checked_at: datetime = Field(default_factory=datetime.utcnow)


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


def latest_source_health() -> list[SourceHealth]:
    """One row per source: the most recent health check recorded for it, across all runs."""
    with get_session() as session:
        rows = session.exec(select(SourceHealth).order_by(SourceHealth.checked_at.desc())).all()
    seen: dict[str, SourceHealth] = {}
    for row in rows:
        seen.setdefault(row.source, row)
    return list(seen.values())
