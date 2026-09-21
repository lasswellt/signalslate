"""
Tests for the collector-control helpers in pipeline.db: keyset item paging, counts, cursor reset,
failure clearing and the last-attempt columns.

record_attempt shares a row with the watermark, so most of its tests assert what it must NOT change.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import SQLModel, Session, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import CollectedItem, Run  # noqa: E402

WATERMARK = datetime(2026, 1, 2, 3, 4, 5)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(engine, source: str, count: int, item_type: str = "mail") -> list[int]:
    """Insert `count` items and return their ids in insertion (ascending) order."""
    with Session(engine) as session:
        run = Run(trigger="manual")
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        ids = []
        for n in range(count):
            item = CollectedItem(
                run_id=run.id,
                source=source,
                item_type=item_type,
                external_id=f"{source}-{item_type}-{n}",
                occurred_at=datetime(2026, 1, 1, 0, n % 60),
                payload="{}",
            )
            session.add(item)
            session.commit()
            session.refresh(item)
            assert item.id is not None
            ids.append(item.id)
    return ids


def test_list_items_newest_first(temp_db):
    ids = _seed(temp_db, "zoom", 5)
    assert [i.id for i in db.list_items("zoom")] == list(reversed(ids))


def test_list_items_empty_source(temp_db):
    _seed(temp_db, "zoom", 3)
    assert db.list_items("nobody") == []
    assert db.list_items("nobody", before_id=10) == []


def test_list_items_ignores_other_sources(temp_db):
    _seed(temp_db, "zoom", 2)
    other = _seed(temp_db, "slack_team", 2)
    assert {i.id for i in db.list_items("slack_team")} == set(other)


def test_list_items_before_id_is_exclusive_and_pages_without_gaps(temp_db):
    ids = _seed(temp_db, "zoom", 7)
    newest_first = list(reversed(ids))

    first = db.list_items("zoom", limit=3)
    assert [i.id for i in first] == newest_first[:3]

    second = db.list_items("zoom", before_id=first[-1].id, limit=3)
    assert [i.id for i in second] == newest_first[3:6]
    assert first[-1].id not in [i.id for i in second]

    third = db.list_items("zoom", before_id=second[-1].id, limit=3)
    assert [i.id for i in third] == newest_first[6:]

    assert db.list_items("zoom", before_id=third[-1].id, limit=3) == []


def test_list_items_before_id_at_oldest_returns_nothing(temp_db):
    ids = _seed(temp_db, "zoom", 3)
    assert db.list_items("zoom", before_id=ids[0]) == []


def test_list_items_filters_by_item_type(temp_db):
    mails = _seed(temp_db, "m365_work", 3, "mail")
    events = _seed(temp_db, "m365_work", 2, "event")
    assert {i.id for i in db.list_items("m365_work", item_type="event")} == set(events)
    assert {i.id for i in db.list_items("m365_work", item_type="mail")} == set(mails)
    assert len(db.list_items("m365_work")) == 5


def test_list_items_limit_clamped_low(temp_db):
    _seed(temp_db, "zoom", 3)
    assert len(db.list_items("zoom", limit=0)) == 1
    assert len(db.list_items("zoom", limit=-5)) == 1


def test_list_items_limit_clamped_high(temp_db):
    _seed(temp_db, "zoom", 205)
    assert len(db.list_items("zoom", limit=10_000)) == 200
    assert len(db.list_items("zoom", limit=200)) == 200
    assert len(db.list_items("zoom", limit=201)) == 200


def test_list_items_default_limit_is_50(temp_db):
    _seed(temp_db, "zoom", 60)
    assert len(db.list_items("zoom")) == 50


def test_count_items(temp_db):
    _seed(temp_db, "m365_work", 3, "mail")
    _seed(temp_db, "m365_work", 2, "event")
    _seed(temp_db, "zoom", 4, "meeting")
    assert db.count_items("m365_work") == 5
    assert db.count_items("m365_work", "mail") == 3
    assert db.count_items("m365_work", item_type="event") == 2
    assert db.count_items("m365_work", "task") == 0
    assert db.count_items("nobody") == 0


def test_reset_cursor_none_deletes_row(temp_db):
    db.set_cursor("zoom", WATERMARK, cursor="abc")
    db.reset_cursor("zoom", None)
    assert db.get_cursor("zoom") is None


def test_reset_cursor_none_on_absent_row_is_a_noop(temp_db):
    db.reset_cursor("zoom", None)
    assert db.get_cursor("zoom") is None


def test_reset_cursor_datetime_sets_watermark_and_zeroes_failures(temp_db):
    db.set_cursor("zoom", WATERMARK)
    db.record_failure("zoom")
    db.record_failure("zoom")
    earlier = datetime(2025, 12, 1, 0, 0)

    db.reset_cursor("zoom", earlier)

    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_success_at == earlier
    assert row.consecutive_failures == 0


def test_reset_cursor_datetime_creates_row_when_absent(temp_db):
    db.reset_cursor("zoom", WATERMARK)
    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_success_at == WATERMARK
    assert row.consecutive_failures == 0


def test_reset_cursor_datetime_matches_set_cursor(temp_db):
    db.set_cursor("a", WATERMARK)
    db.reset_cursor("b", WATERMARK)
    a, b = db.get_cursor("a"), db.get_cursor("b")
    assert a is not None and b is not None
    assert (a.last_success_at, a.cursor, a.consecutive_failures) == (b.last_success_at, b.cursor, b.consecutive_failures)


def test_clear_failures_keeps_watermark(temp_db):
    db.set_cursor("zoom", WATERMARK, cursor="abc")
    db.record_failure("zoom")
    db.record_failure("zoom")
    before = db.get_cursor("zoom")
    assert before is not None and before.consecutive_failures == 2

    db.clear_failures("zoom")

    row = db.get_cursor("zoom")
    assert row is not None
    assert row.consecutive_failures == 0
    assert row.last_success_at == WATERMARK
    assert row.cursor == "abc"


def test_clear_failures_on_absent_row_creates_nothing(temp_db):
    db.clear_failures("zoom")
    assert db.get_cursor("zoom") is None


def test_record_attempt_creates_row_when_absent(temp_db):
    at = datetime(2026, 2, 3, 4, 5, 6)
    db.record_attempt("zoom", "error", "boom", 0, at=at)

    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_attempt_at == at
    assert row.last_status == "error"
    assert row.last_detail == "boom"
    assert row.last_item_count == 0
    assert row.last_success_at is None
    assert row.consecutive_failures == 0


def test_record_attempt_never_disturbs_watermark_or_failures(temp_db):
    db.set_cursor("zoom", WATERMARK, cursor="abc")
    db.record_failure("zoom")
    db.record_failure("zoom")
    db.record_failure("zoom")

    db.record_attempt("zoom", "error", "upstream 500", 0, at=datetime(2026, 2, 3, 4, 5, 6))

    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_success_at == WATERMARK
    assert row.cursor == "abc"
    assert row.consecutive_failures == 3
    assert row.last_status == "error"


def test_record_attempt_overwrites_previous_attempt(temp_db):
    db.record_attempt("zoom", "error", "first", 0, at=datetime(2026, 2, 3, 0, 0))
    db.record_attempt("zoom", "ok", None, 12, at=datetime(2026, 2, 4, 0, 0))

    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_attempt_at == datetime(2026, 2, 4, 0, 0)
    assert row.last_status == "ok"
    assert row.last_detail is None
    assert row.last_item_count == 12


def test_record_attempt_defaults_at_to_utcnow(temp_db, monkeypatch):
    fixed = datetime(2026, 5, 6, 7, 8, 9)
    monkeypatch.setattr(db, "utcnow", lambda: fixed)
    db.record_attempt("zoom", "ok", None, 1)
    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_attempt_at == fixed


def test_record_attempt_truncates_detail_to_500_chars(temp_db):
    db.record_attempt("zoom", "error", "x" * 900, 0)
    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_detail == "x" * 500

    db.record_attempt("zoom", "error", "y" * 500, 0)
    row = db.get_cursor("zoom")
    assert row is not None
    assert row.last_detail == "y" * 500

