"""
Tests for the Connection / Tombstone tables and the SourceCursor last-attempt columns in pipeline.db.

The migration test builds the OLD SourceCursor schema with raw SQL: create_all() never alters an
existing table, so only a database that predates the columns proves _add_missing_columns() works.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import Connection, SourceCursor, Tombstone  # noqa: E402

NEW_COLUMNS = {"last_attempt_at", "last_status", "last_detail", "last_item_count"}


def _columns(engine, table: str) -> dict[str, int]:
    """{column name: notnull flag} straight from SQLite, independent of the models."""
    with engine.connect() as conn:
        return {row[1]: row[3] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def test_create_all_makes_both_tables(temp_db):
    assert {"connection", "tombstone"} <= set(SQLModel.metadata.tables)
    assert {"id", "kind", "label", "origin", "seq", "config", "secret_ciphertext", "created_at", "updated_at"} == set(
        _columns(temp_db, "connection")
    )
    assert {"id", "deleted_at"} == set(_columns(temp_db, "tombstone"))
    assert NEW_COLUMNS <= set(_columns(temp_db, "sourcecursor"))


def test_new_sourcecursor_columns_are_nullable():
    cols = SQLModel.metadata.tables["sourcecursor"].columns
    for name in NEW_COLUMNS:
        assert cols[name].nullable, f"{name} must be nullable or the migration helper skips it"


def test_old_sourcecursor_schema_gains_columns_and_keeps_rows(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE sourcecursor (source VARCHAR NOT NULL, last_success_at DATETIME, cursor VARCHAR, "
        "consecutive_failures INTEGER NOT NULL, PRIMARY KEY (source))"
    )
    raw.execute(
        "INSERT INTO sourcecursor (source, last_success_at, cursor, consecutive_failures) "
        "VALUES ('zoom', '2026-01-02 03:04:05.000000', NULL, 2)"
    )
    raw.commit()
    raw.close()

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    assert NEW_COLUMNS.isdisjoint(_columns(engine, "sourcecursor"))

    SQLModel.metadata.create_all(engine)
    db._add_missing_columns()

    cols = _columns(engine, "sourcecursor")
    assert NEW_COLUMNS <= set(cols)
    assert all(cols[name] == 0 for name in NEW_COLUMNS)
    assert "connection" in SQLModel.metadata.tables and _columns(engine, "connection")

    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_success_at == datetime(2026, 1, 2, 3, 4, 5)
    assert row.consecutive_failures == 2
    assert (row.last_attempt_at, row.last_status, row.last_detail, row.last_item_count) == (None, None, None, None)

    with Session(engine) as session:
        row.last_attempt_at = datetime(2026, 1, 3)
        row.last_status = "error"
        row.last_detail = "boom"
        row.last_item_count = 0
        session.add(row)
        session.commit()
    again = db.get_cursor("zoom")
    assert again is not None
    assert (again.last_status, again.last_detail, again.last_item_count) == ("error", "boom", 0)


def test_migration_is_idempotent(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'again.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    before = _columns(engine, "sourcecursor")
    db._add_missing_columns()
    db._add_missing_columns()
    assert _columns(engine, "sourcecursor") == before


def test_connection_round_trip_with_and_without_ciphertext(temp_db):
    with Session(temp_db) as session:
        session.add(Connection(id="m365_acme", kind="m365", label="acme", origin="ui", seq=1, config='{"tenant": "example.com"}'))
        session.add(
            Connection(
                id="slack_ops", kind="slack", label="ops", origin="env", config="{}", secret_ciphertext="opaque-envelope"
            )
        )
        session.commit()

    with Session(temp_db) as session:
        plain = session.get(Connection, "m365_acme")
        sealed = session.get(Connection, "slack_ops")
    assert plain is not None and sealed is not None
    assert plain.secret_ciphertext is None
    assert plain.config == '{"tenant": "example.com"}'
    assert (plain.kind, plain.label, plain.origin, plain.seq) == ("m365", "acme", "ui", 1)
    assert isinstance(plain.created_at, datetime) and isinstance(plain.updated_at, datetime)
    assert plain.created_at.tzinfo is None
    assert sealed.secret_ciphertext == "opaque-envelope"
    assert sealed.seq == 0


def test_tombstone_primary_key_is_unique(temp_db):
    with Session(temp_db) as session:
        session.add(Tombstone(id="zoom"))
        session.commit()
        stamped = session.exec(select(Tombstone)).one()
        assert stamped.deleted_at.tzinfo is None

    with Session(temp_db) as session:
        session.add(Tombstone(id="zoom"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_init_db_migrates_existing_file(monkeypatch, tmp_path):
    path = tmp_path / "data" / "digest.db"
    path.parent.mkdir()
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE sourcecursor (source VARCHAR NOT NULL, last_success_at DATETIME, cursor VARCHAR, "
        "consecutive_failures INTEGER NOT NULL, PRIMARY KEY (source))"
    )
    raw.execute("INSERT INTO sourcecursor VALUES ('slack_ops', NULL, NULL, 0)")
    raw.commit()
    raw.close()
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", path)

    db.init_db()

    assert NEW_COLUMNS <= set(_columns(engine, "sourcecursor"))
    assert {"connection", "tombstone"} <= set(
        row[0] for row in engine.connect().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
    )
    kept = db.get_cursor("slack_ops")
    assert isinstance(kept, SourceCursor) and kept.consecutive_failures == 0
