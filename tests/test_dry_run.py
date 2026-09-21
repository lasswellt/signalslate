"""
Tests for pipeline.collect.dry_run, the pure data half of the collector dry run, and for report(),
the CLI printer that now sits on top of the same code.

Only the collector call (dispatch, the network edge) and the clock are stubbed. The source list is
the real known_sources(): "zoom" is always declared, Slack workspaces come from a tmp .env.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import collect, db, health  # noqa: E402
from pipeline.collectors import CollectionResult, Item  # noqa: E402

NOW = datetime(2026, 9, 21, 10, 5, 30)
OCCURRED = datetime(2026, 9, 21, 9, 15, 59)
SECRET = "xoxp-do-not-leak-0123456789"


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(collect, "utcnow", lambda: NOW)


@pytest.fixture
def declared(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("SLACK_ALPHA_TOKEN=xoxp-a\n")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def stub_dispatch(monkeypatch, result):
    """Stub collect.dispatch; `result` is a CollectionResult or an exception to raise. Returns the recorded windows."""
    windows: list[tuple[datetime, datetime]] = []

    def fake(source, since, until):
        windows.append((since, until))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(collect, "dispatch", fake)
    return windows


def five_items() -> list[Item]:
    return [
        Item("mail", "m1", OCCURRED, {"subject": "First\nline", "extra": "é" * 3}),
        Item("mail", "m2", OCCURRED, {"text": "Second"}),
        Item("chat", "c1", OCCURRED, {"body": {"content": "Hello  chat"}}),
        Item("event", "e1", OCCURRED, {"nothing": 1, "when": OCCURRED}),
        Item("mail", "m3", OCCURRED, {"subject": "Third", "blob": "x" * 5000}),
    ]


# --- result shape ---------------------------------------------------------------------


def test_ok_result_has_the_documented_shape(monkeypatch):
    windows = stub_dispatch(monkeypatch, CollectionResult("zoom", "ok", "5 things", five_items()))

    data = collect.dry_run("zoom", hours=24, limit=3)

    assert set(data) == {"source", "status", "detail", "window", "count", "by_type", "items", "duration_ms"}
    assert data["source"] == "zoom" and data["status"] == "ok" and data["detail"] == "5 things"
    assert data["window"] == {"since": NOW - timedelta(hours=24), "until": NOW, "hours": 24}
    assert windows == [(NOW - timedelta(hours=24), NOW)]
    assert data["count"] == 5
    assert data["by_type"] == {"mail": 3, "chat": 1, "event": 1}
    assert isinstance(data["duration_ms"], int) and data["duration_ms"] >= 0
    assert data["items"] == [
        {"item_type": "mail", "occurred_at": OCCURRED, "external_id": "m1", "preview": "First line"},
        {"item_type": "mail", "occurred_at": OCCURRED, "external_id": "m2", "preview": "Second"},
        {"item_type": "chat", "occurred_at": OCCURRED, "external_id": "c1", "preview": "Hello  chat"},
    ]
    assert all("payload" not in item for item in data["items"])


def test_partial_result_passes_status_and_detail_through(monkeypatch):
    stub_dispatch(monkeypatch, CollectionResult("zoom", "partial", "rate limited", five_items()[:1]))

    data = collect.dry_run("zoom")

    assert data["status"] == "partial" and data["detail"] == "rate limited"
    assert data["count"] == 1 and len(data["items"]) == 1


def test_error_result_is_returned_not_raised(monkeypatch):
    stub_dispatch(monkeypatch, CollectionResult("zoom", "error", "dead token"))

    data = collect.dry_run("zoom")

    assert data["status"] == "error" and data["detail"] == "dead token"
    assert data["count"] == 0 and data["by_type"] == {} and data["items"] == []


def test_crash_reports_the_class_name_only(monkeypatch):
    stub_dispatch(monkeypatch, RuntimeError(f"upstream said {SECRET}"))

    data = collect.dry_run("zoom")

    assert data["status"] == "crashed" and data["detail"] == "RuntimeError"
    assert data["count"] == 0 and data["items"] == [] and data["by_type"] == {}
    assert SECRET not in repr(data)


# --- argument handling ----------------------------------------------------------------


@pytest.mark.parametrize(
    "hours, expected", [(0, 1), (-5, 1), (1, 1), (24, 24), (168, 168), (169, 168), (10_000, 168)]
)
def test_hours_is_clamped_to_1_through_168(monkeypatch, hours, expected):
    windows = stub_dispatch(monkeypatch, CollectionResult("zoom", "ok", "", []))

    data = collect.dry_run("zoom", hours=hours)

    assert data["window"]["hours"] == expected
    assert windows == [(NOW - timedelta(hours=expected), NOW)]


@pytest.mark.parametrize("limit, expected", [(-3, 0), (0, 0), (1, 1), (50, 50), (51, 50), (500, 50)])
def test_limit_is_clamped_to_0_through_50(monkeypatch, limit, expected):
    items = [Item("mail", f"m{i}", OCCURRED, {"subject": f"s{i}"}) for i in range(60)]
    stub_dispatch(monkeypatch, CollectionResult("zoom", "ok", "", items))

    data = collect.dry_run("zoom", limit=limit)

    assert len(data["items"]) == expected
    assert data["count"] == 60 and data["by_type"] == {"mail": 60}


def test_unknown_source_raises_value_error_before_any_collector_call(monkeypatch):
    windows = stub_dispatch(monkeypatch, CollectionResult("nope", "ok", "", []))

    with pytest.raises(ValueError, match="nope"):
        collect.dry_run("nope")
    assert windows == []


def test_declared_slack_source_is_accepted(monkeypatch, declared):
    stub_dispatch(monkeypatch, CollectionResult("slack_alpha", "ok", "fine", []))

    assert collect.dry_run("slack_alpha")["status"] == "ok"


# --- raw payloads ---------------------------------------------------------------------


def test_raw_adds_the_payload_truncated_to_4000_chars(monkeypatch):
    stub_dispatch(monkeypatch, CollectionResult("zoom", "ok", "", five_items()))

    data = collect.dry_run("zoom", limit=5, raw=True)

    payloads = [item["payload"] for item in data["items"]]
    assert payloads[0] == '{\n  "subject": "First\\nline",\n  "extra": "\\u00e9\\u00e9\\u00e9"\n}'
    assert '"when": "2026-09-21 09:15:59"' in payloads[3]  # default=str, as the CLI has always done
    assert len(payloads[4]) == 4000 and payloads[4].startswith('{\n  "subject": "Third"')
    assert all(len(p) <= 4000 for p in payloads)


# --- writes nothing -------------------------------------------------------------------


def test_dry_run_writes_no_rows(monkeypatch, temp_db):
    def counts() -> dict[str, int]:
        with temp_db.connect() as conn:
            return {t.name: conn.execute(select(func.count()).select_from(t)).scalar_one() for t in SQLModel.metadata.sorted_tables}

    before = counts()
    assert before  # the schema exists, so an all-empty comparison below is not vacuous
    stub_dispatch(monkeypatch, CollectionResult("zoom", "ok", "5 things", five_items()))
    collect.dry_run("zoom", raw=True)
    stub_dispatch(monkeypatch, RuntimeError("boom"))
    collect.dry_run("zoom")

    assert counts() == before


# --- report() output ------------------------------------------------------------------


def test_report_output_is_unchanged(monkeypatch, capsys):
    stub_dispatch(monkeypatch, CollectionResult("m365_work", "ok", "5 things", five_items()))

    assert collect.report("m365_work", hours=24, limit=3, raw=False) is True

    assert capsys.readouterr().out == (
        "\n=== m365_work ===\n"
        "window: 2026-09-20 10:05 .. 2026-09-21 10:05 UTC (24h)\n"
        "status: OK\n"
        "detail: 5 things\n"
        "items:  5\n"
        "        chat=1, event=1, mail=3\n"
        "\n  [mail] 2026-09-21 09:15 id=m1\n"
        "  First line\n"
        "\n  [mail] 2026-09-21 09:15 id=m2\n"
        "  Second\n"
        "\n  [chat] 2026-09-21 09:15 id=c1\n"
        "  Hello  chat\n"
        "\n  ... 2 more (raise --limit to see them)\n"
    )


def test_report_raw_output_is_unchanged(monkeypatch, capsys):
    stub_dispatch(monkeypatch, CollectionResult("zoom", "partial", "rate limited", five_items()[:2]))

    assert collect.report("zoom", hours=6, limit=5, raw=True) is True

    assert capsys.readouterr().out == (
        "\n=== zoom ===\n"
        "window: 2026-09-21 04:05 .. 2026-09-21 10:05 UTC (6h)\n"
        "status: PARTIAL\n"
        "detail: rate limited\n"
        "items:  2\n"
        "        mail=2\n"
        "\n  [mail] 2026-09-21 09:15 id=m1\n"
        '{\n  "subject": "First\\nline",\n  "extra": "\\u00e9\\u00e9\\u00e9"\n}\n'
        "\n  [mail] 2026-09-21 09:15 id=m2\n"
        '{\n  "text": "Second"\n}\n'
    )


def test_report_error_and_crash_output_are_unchanged(monkeypatch, capsys):
    stub_dispatch(monkeypatch, CollectionResult("zoom", "error", "dead token"))
    assert collect.report("zoom", hours=24, limit=5, raw=False) is False
    assert capsys.readouterr().out == (
        "\n=== zoom ===\n"
        "window: 2026-09-20 10:05 .. 2026-09-21 10:05 UTC (24h)\n"
        "status: ERROR\n"
        "detail: dead token\n"
        "items:  0\n"
    )

    stub_dispatch(monkeypatch, RuntimeError("unexpected shape"))
    assert collect.report("zoom", hours=24, limit=5, raw=False) is False
    assert capsys.readouterr().out == (
        "\n=== zoom ===\n"
        "window: 2026-09-20 10:05 .. 2026-09-21 10:05 UTC (24h)\n"
        "status: CRASHED — RuntimeError: unexpected shape\n"
    )


def test_report_does_not_clamp_what_the_cli_never_clamped(monkeypatch, capsys):
    stub_dispatch(monkeypatch, CollectionResult("zoom", "ok", "", []))

    collect.report("zoom", hours=500, limit=5, raw=False)

    assert "window: 2026-08-31 14:05 .. 2026-09-21 10:05 UTC (500h)" in capsys.readouterr().out
