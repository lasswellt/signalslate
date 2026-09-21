"""
pipeline.triage_cli: the dry-run of the map call over collected Gmail items.

Rows are built from the REAL flatten_message() output for the synthetic multipart fixture, so the CLI
is exercised on the payload shape the collector actually stores. The Anthropic API is the only true
external, so the live path uses a FakeClient (the pattern from test_triage_client.py).
"""
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, triage_cli  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors.gmail import flatten_message  # noqa: E402
from pipeline.triage import Category, Importance, TriageBatch, TriageConfigError, TriageRecord  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "gmail_message_multipart.json"
SOURCE = "gmail_test"
EXTERNAL_IDS = ["ext-secret-1", "ext-secret-2", "ext-secret-3"]


def flat_payload() -> dict[str, Any]:
    return flatten_message(json.loads(FIXTURE.read_text()))


def make_row(source: str, external_id: str, occurred_at, payload: str | None = None) -> db.CollectedItem:
    return db.CollectedItem(
        run_id=1,
        source=source,
        item_type="mail",
        external_id=external_id,
        occurred_at=occurred_at,
        payload=payload if payload is not None else json.dumps(flat_payload()),
    )


def seed(engine, rows: list[db.CollectedItem]) -> None:
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()


def row_counts(engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            model.__name__: len(session.exec(select(model)).all())
            for model in (db.Run, db.SourceHealth, db.CollectedItem, db.SourceCursor)
        }


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def seeded(temp_db):
    now = utcnow()
    seed(temp_db, [make_row(SOURCE, ext, now - timedelta(hours=n)) for n, ext in enumerate(EXTERNAL_IDS, start=1)])
    return temp_db


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        triage_cli, "make_client", lambda: pytest.fail("make_client must not be called in this test")
    )


class FakeClient:
    """Answers every parse() with one record per alias it was sent, or with a scripted failure."""

    def __init__(self, fail: bool = False, one_line: str | None = None) -> None:
        self.fail = fail
        self.one_line = one_line
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail:
            return SimpleNamespace(parsed_output=None, stop_reason="refusal")
        aliases = [i["alias"] for i in json.loads(kwargs["messages"][0]["content"])["items"]]
        records = [
            TriageRecord(
                id=alias,
                category=Category.ACTION,
                importance=Importance.HIGH,
                one_line=self.one_line or f"Summary for {alias}.",
                action_needed=True,
                action_text="Reply to the sender",
                due="2026-10-01",
                people=["Alex Example"],
            )
            for alias in aliases
        ]
        return SimpleNamespace(parsed_output=TriageBatch(records=records), stop_reason="end_turn")


def hostile_row(external_id: str, occurred_at) -> db.CollectedItem:
    """A stored row whose title and body hold a lone surrogate: the ASCII-escaped JSON "\\ud800" loads back as one."""
    payload = {**flat_payload(), "subject": "Hostile \ud800 subject", "bodyText": "Hostile \ud800 body"}
    return make_row(SOURCE, external_id, occurred_at, json.dumps(payload))


def use_client(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(triage_cli, "make_client", lambda: client)


# --- items_for_source_since ------------------------------------------------------------------------


def test_helper_filters_by_source_and_time_and_orders_ascending(temp_db):
    now = utcnow()
    seed(
        temp_db,
        [
            make_row(SOURCE, "newest", now - timedelta(hours=1)),
            make_row(SOURCE, "oldest-in-window", now - timedelta(hours=5)),
            make_row(SOURCE, "too-old", now - timedelta(hours=30)),
            make_row("gmail_other", "other-source", now - timedelta(hours=2)),
        ],
    )
    rows = db.items_for_source_since(SOURCE, now - timedelta(hours=24))
    assert [r.external_id for r in rows] == ["oldest-in-window", "newest"]


def test_helper_includes_the_boundary(temp_db):
    since = utcnow() - timedelta(hours=3)
    seed(temp_db, [make_row(SOURCE, "on-the-line", since)])
    assert [r.external_id for r in db.items_for_source_since(SOURCE, since)] == ["on-the-line"]


# --- --dry -----------------------------------------------------------------------------------------


def test_dry_prints_valid_json_with_schema_and_alias_content(seeded, no_key, capsys):
    assert triage_cli.main([SOURCE, "--dry"]) == 0
    out = capsys.readouterr().out

    request = json.loads(out[out.index("{") :])
    assert request["output_format"] == TriageBatch.model_json_schema()
    content = request["messages"][0]["content"]
    assert [i["alias"] for i in content["items"]] == ["m001", "m002", "m003"]
    assert all(i["title"] and i["body_text"] for i in content["items"])
    assert "model:" in out and "3 in 1 batch(es)" in out


def test_dry_never_prints_real_external_ids(seeded, no_key, capsys):
    triage_cli.main([SOURCE, "--dry"])
    out = capsys.readouterr().out
    assert not any(ext in out for ext in EXTERNAL_IDS)
    assert SOURCE + ":" not in out


def test_dry_shows_only_the_first_batch(seeded, no_key, capsys):
    assert triage_cli.main([SOURCE, "--dry", "--batch-size", "2"]) == 0
    out = capsys.readouterr().out
    content = json.loads(out[out.index("{") :])["messages"][0]["content"]
    assert len(content["items"]) == 2
    assert "3 in 2 batch(es) of up to 2" in out


def test_dry_prints_a_lone_surrogate_as_an_escape_instead_of_crashing(temp_db, no_key, capsys):
    seed(temp_db, [hostile_row("hostile-1", utcnow() - timedelta(hours=1))])
    assert triage_cli.main([SOURCE, "--dry"]) == 0
    captured = capsys.readouterr()
    assert "Hostile \\ud800 subject" in captured.out
    assert "Hostile \\ud800 body" in captured.out
    assert "Traceback" not in captured.out + captured.err
    json.loads(captured.out[captured.out.index("{") :])


def test_dry_writes_nothing(seeded, no_key):
    # All four tables, and the seeded rows themselves: a dry run that stores a cursor, health or Run
    # row, or rewrites an item, must fail here, not only the live path.
    def snapshot():
        with Session(seeded) as session:
            return [(row.id, row.external_id, row.payload, row.occurred_at) for row in session.exec(select(db.CollectedItem))]

    counts, rows = row_counts(seeded), snapshot()
    assert triage_cli.main([SOURCE, "--dry", "--batch-size", "2"]) == 0
    assert row_counts(seeded) == counts == {"Run": 0, "SourceHealth": 0, "CollectedItem": 3, "SourceCursor": 0}
    assert snapshot() == rows


# --- live ------------------------------------------------------------------------------------------


def test_live_prints_records_and_returns_zero(seeded, monkeypatch, capsys):
    client = FakeClient()
    use_client(monkeypatch, client)
    assert triage_cli.main([SOURCE]) == 0
    out = capsys.readouterr().out

    assert "WARNING" in out and "Anthropic" in out
    assert out.index("WARNING") < out.index(f"{SOURCE}:ext-secret")
    assert "action / high: Summary for m001." in out
    assert "action: Reply to the sender (due 2026-10-01)" in out
    assert "stubbed: 0 of 3" in out
    assert len(client.calls) == 1


def test_live_limit_caps_printed_records(seeded, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient())
    assert triage_cli.main([SOURCE, "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert out.count("Summary for") == 1
    assert "2 more" in out


def test_live_all_stubbed_returns_one(seeded, monkeypatch, capsys):
    use_client(monkeypatch, FakeClient(fail=True))
    assert triage_cli.main([SOURCE]) == 1
    out = capsys.readouterr().out
    assert "(stub)" in out
    assert "stubbed: 3 of 3" in out
    assert "failure: batch 1" in out


def test_live_config_error_is_a_message_not_a_traceback(seeded, monkeypatch, capsys):
    def no_key_configured():
        raise TriageConfigError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(triage_cli, "make_client", no_key_configured)
    assert triage_cli.main([SOURCE]) == 2
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY is not set" in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert "WARNING" not in captured.out


def test_live_run_writes_nothing(seeded, monkeypatch):
    before = row_counts(seeded)
    use_client(monkeypatch, FakeClient())
    assert triage_cli.main([SOURCE]) == 0
    assert row_counts(seeded) == before == {"Run": 0, "SourceHealth": 0, "CollectedItem": 3, "SourceCursor": 0}


def test_live_prints_a_lone_surrogate_from_the_model_and_the_item_without_crashing(temp_db, monkeypatch, capsys):
    seed(temp_db, [hostile_row("hostile-1", utcnow() - timedelta(hours=1))])
    use_client(monkeypatch, FakeClient(one_line="Model says \ud800 here"))
    assert triage_cli.main([SOURCE]) == 0
    captured = capsys.readouterr()
    assert "Model says \\ud800 here" in captured.out
    assert "stubbed: 0 of 1" in captured.out
    assert "Traceback" not in captured.out + captured.err


# --- refusals and empty cases ----------------------------------------------------------------------


def test_missing_database_returns_two_and_creates_no_file(monkeypatch, tmp_path, no_key, capsys):
    path = tmp_path / "absent.db"
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    assert triage_cli.main([SOURCE, "--dry"]) == 2
    assert "no database yet" in capsys.readouterr().out
    assert not path.exists()


def test_source_without_adapter_returns_two(seeded, no_key, capsys):
    assert triage_cli.main(["slack_work", "--dry"]) == 2
    assert "no normalizer" in capsys.readouterr().out


def test_empty_window_returns_zero(temp_db, no_key, capsys):
    seed(temp_db, [make_row(SOURCE, "old", utcnow() - timedelta(hours=48))])
    assert triage_cli.main([SOURCE, "--hours", "24"]) == 0
    assert "no gmail_test items in the last 24h" in capsys.readouterr().out


def test_unreadable_payloads_are_skipped_and_counted(temp_db, no_key, capsys):
    now = utcnow()
    seed(temp_db, [make_row(SOURCE, "good", now - timedelta(hours=1)), make_row(SOURCE, "bad", now, "not json")])
    assert triage_cli.main([SOURCE, "--dry"]) == 0
    out = capsys.readouterr().out
    assert "skipped 1 row(s)" in out
    assert "1 in 1 batch(es)" in out


def test_rejects_non_positive_arguments(seeded, no_key):
    with pytest.raises(SystemExit) as exc:
        triage_cli.main([SOURCE, "--batch-size", "0"])
    assert exc.value.code == 2
