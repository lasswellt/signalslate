"""
Tests for pipeline.jobs.inbox_seed: job-alert sender filtering, LLM extraction, and the
add_company()/match_any_url() wiring in seed_from_inbox(). Real (in-memory) SQLite session for
CollectedItem/JobCompany per tests/test_db_collector_helpers.py's pattern; the model call is a
scripted FakeClient per tests/test_domain_ideas.py's style — no real network/API calls.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import CollectedItem, JobCompany, Run  # noqa: E402
from pipeline.jobs import inbox_seed  # noqa: E402
from pipeline.triage import TriageConfigError  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        run = Run(trigger="manual")
        session.add(run)
        session.commit()
        session.refresh(run)
        session.info["run_id"] = run.id
        yield session


def _add_item(session, *, from_header, subject="", body_text="", occurred_at=None, item_type="mail"):
    payload = {"subject": subject, "from": from_header, "bodyText": body_text}
    item = CollectedItem(
        run_id=session.info["run_id"],
        source="gmail_personal",
        item_type=item_type,
        external_id=f"ext-{from_header}-{subject}",
        occurred_at=occurred_at or datetime(2026, 1, 1, 12, 0, 0),
        payload=json.dumps(payload),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic: each parse() pops the next response or raises it."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.messages = SimpleNamespace(parse=self._parse)
        self.calls: list[dict] = []

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=cast(Any, SimpleNamespace(method="POST", url="https://example.com/v1/messages"))
    )


def alert_reply(*entries: dict, stop_reason: str = "end_turn") -> SimpleNamespace:
    parsed = inbox_seed._JobAlertBatch(
        entries=[
            inbox_seed._JobAlertRecord(
                company=entry.get("company"), title=entry.get("title"), link=entry.get("link")
            )
            for entry in entries
        ]
    )
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


SINCE = datetime(2026, 1, 1, 0, 0, 0)
UNTIL = datetime(2026, 1, 2, 0, 0, 0)


# --------------------------------------------------------------------------------------------
# select_job_alert_items
# --------------------------------------------------------------------------------------------


def test_select_job_alert_items_matches_known_domain(session):
    matched = _add_item(session, from_header="LinkedIn Job Alerts <jobs-noreply@linkedin.com>")
    _add_item(session, from_header="Someone <person@example.com>")  # not a known sender

    result = inbox_seed.select_job_alert_items(session, SINCE, UNTIL)
    assert [row.id for row in result] == [matched.id]


def test_select_job_alert_items_matches_subdomain_case_insensitive(session):
    matched = _add_item(session, from_header="alerts@Jobs.INDEED.com")
    result = inbox_seed.select_job_alert_items(session, SINCE, UNTIL)
    assert [row.id for row in result] == [matched.id]


def test_select_job_alert_items_excludes_outside_window(session):
    _add_item(session, from_header="a@linkedin.com", occurred_at=datetime(2025, 12, 31, 23, 59, 59))
    _add_item(session, from_header="a@linkedin.com", occurred_at=datetime(2026, 1, 2, 0, 0, 0))
    result = inbox_seed.select_job_alert_items(session, SINCE, UNTIL)
    assert result == []


def test_select_job_alert_items_excludes_non_mail_item_type(session):
    _add_item(session, from_header="a@linkedin.com", item_type="event")
    result = inbox_seed.select_job_alert_items(session, SINCE, UNTIL)
    assert result == []


def test_select_job_alert_items_skips_bad_json_payload(session):
    item = CollectedItem(
        run_id=session.info["run_id"],
        source="gmail_personal",
        item_type="mail",
        external_id="bad-json",
        occurred_at=datetime(2026, 1, 1, 12, 0, 0),
        payload="not json",
    )
    session.add(item)
    session.commit()
    result = inbox_seed.select_job_alert_items(session, SINCE, UNTIL)
    assert result == []


def test_select_job_alert_items_skips_missing_from(session):
    item = CollectedItem(
        run_id=session.info["run_id"],
        source="gmail_personal",
        item_type="mail",
        external_id="no-from",
        occurred_at=datetime(2026, 1, 1, 12, 0, 0),
        payload=json.dumps({"subject": "hi"}),
    )
    session.add(item)
    session.commit()
    result = inbox_seed.select_job_alert_items(session, SINCE, UNTIL)
    assert result == []


# --------------------------------------------------------------------------------------------
# extract_from_email
# --------------------------------------------------------------------------------------------


def test_extract_from_email_returns_entries():
    client = FakeClient(
        alert_reply(
            {"company": "Acme Corp", "title": "Backend Engineer", "link": "https://boards.greenhouse.io/acme/1"},
            {"company": "Beta Inc", "title": None, "link": None},
        )
    )
    entries, reason = inbox_seed.extract_from_email("Job alert", "body", client=client, model="test-model")
    assert reason == ""
    assert entries == [
        {"company": "Acme Corp", "title": "Backend Engineer", "link": "https://boards.greenhouse.io/acme/1"},
        {"company": "Beta Inc", "title": None, "link": None},
    ]
    assert client.calls[0]["model"] == "test-model"
    assert client.calls[0]["output_format"] is inbox_seed._JobAlertBatch


def test_extract_from_email_sanitizes_before_sending():
    client = FakeClient(alert_reply())
    inbox_seed.extract_from_email(
        "Subject <script>x</script>", "Body http://evil.example/x text", client=client, model="test-model"
    )
    sent = json.loads(client.calls[0]["messages"][0]["content"])
    assert "<script>" not in sent["subject"]
    assert "http://" not in sent["body"]


def test_extract_from_email_refusal_degrades_to_empty_with_reason():
    client = FakeClient(alert_reply(stop_reason="refusal"))
    entries, reason = inbox_seed.extract_from_email("s", "b", client=client, model="test-model")
    assert entries == []
    assert reason == "stop_reason=refusal"


def test_extract_from_email_no_parsed_output_degrades_to_empty_with_reason():
    client = FakeClient(SimpleNamespace(parsed_output=None, stop_reason="end_turn"))
    entries, reason = inbox_seed.extract_from_email("s", "b", client=client, model="test-model")
    assert entries == []
    assert reason == "no parsed output"


def test_extract_from_email_api_error_degrades_to_empty_with_class_name_reason():
    client = FakeClient(connection_error())
    entries, reason = inbox_seed.extract_from_email("s", "b", client=client, model="test-model")
    assert entries == []
    assert reason == "APIConnectionError"


def test_extract_from_email_unconfigured_never_raises(monkeypatch):
    def fake_make_client():
        raise TriageConfigError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(inbox_seed, "make_client", fake_make_client)
    entries, reason = inbox_seed.extract_from_email("s", "b")
    assert entries == []
    assert reason == "ANTHROPIC_API_KEY is not set"


# --------------------------------------------------------------------------------------------
# seed_from_inbox
# --------------------------------------------------------------------------------------------


def test_seed_from_inbox_adds_companies_and_hints(session):
    _add_item(
        session,
        from_header="jobs-noreply@linkedin.com",
        subject="New jobs for you",
        body_text="Acme Corp is hiring; Beta Inc is hiring too.",
    )
    client = FakeClient(
        alert_reply(
            {"company": "Acme Corp", "title": "Backend Engineer", "link": "https://boards.greenhouse.io/acme/1"},
            {"company": "Beta Inc", "title": "PM", "link": None},
        )
    )

    result = inbox_seed.seed_from_inbox(session, SINCE, UNTIL, client=client, model="test-model")
    session.commit()

    assert result.added == ["Acme Corp", "Beta Inc"]
    assert result.hints == [("Acme Corp", "greenhouse", "acme")]
    rows = {row.name for row in session.exec(select(JobCompany)).all()}
    assert rows == {"Acme Corp", "Beta Inc"}
    acme = session.exec(select(JobCompany).where(JobCompany.name == "Acme Corp")).one()
    assert acme.source == "email"


def test_seed_from_inbox_skips_entries_without_company(session):
    _add_item(session, from_header="jobs-noreply@linkedin.com", subject="s", body_text="b")
    client = FakeClient(alert_reply({"company": None, "title": "PM", "link": None}, {"company": "  ", "title": None, "link": None}))

    result = inbox_seed.seed_from_inbox(session, SINCE, UNTIL, client=client, model="test-model")
    session.commit()

    assert result.added == []
    assert session.exec(select(JobCompany)).all() == []


def test_seed_from_inbox_records_rejected_on_extraction_failure(session):
    item = _add_item(session, from_header="jobs-noreply@linkedin.com", subject="s", body_text="b")
    client = FakeClient(SimpleNamespace(parsed_output=None, stop_reason="end_turn"))

    result = inbox_seed.seed_from_inbox(session, SINCE, UNTIL, client=client, model="test-model")

    assert result.added == []
    assert result.rejected == [(item.id, "no parsed output")]


def test_seed_from_inbox_caps_emails_per_run(session, monkeypatch):
    monkeypatch.setattr(inbox_seed, "_MAX_INBOX_EMAILS", 1)
    _add_item(session, from_header="a@linkedin.com", subject="s1", body_text="b1")
    _add_item(session, from_header="b@linkedin.com", subject="s2", body_text="b2")

    client = FakeClient(alert_reply({"company": "Acme Corp", "title": None, "link": None}))
    result = inbox_seed.seed_from_inbox(session, SINCE, UNTIL, client=client, model="test-model")
    session.commit()

    assert len(client.calls) == 1
    assert result.added == ["Acme Corp"]


def test_seed_from_inbox_ignores_non_alert_senders(session):
    _add_item(session, from_header="friend@example.com", subject="hi", body_text="hello")
    client = FakeClient()

    result = inbox_seed.seed_from_inbox(session, SINCE, UNTIL, client=client, model="test-model")
    assert result.added == []
    assert client.calls == []
