"""
Tests for pipeline.jobs.packet: Claude cover letter + screening-answer drafts, fpdf2 PDF per
application. Real (in-memory) SQLite session per tests/test_jobs_inventory.py's pattern. The
Anthropic client is a fake (duck-typed messages.parse) — no real network/API call. DB_PATH is
monkeypatched to a tmp directory so render_cover_letter_pdf() never touches the real data/ dir.
"""
import json
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import JobApplication, JobBoard, JobCompany, JobPosting, JobsProfile  # noqa: E402
from pipeline.jobs import packet  # noqa: E402
from pipeline.jobs.packet import (  # noqa: E402
    ScreeningDraft,
    generate_packet,
    prepare_packet,
    render_cover_letter_pdf,
)
from pipeline.triage import TriageConfigError  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class _FakeParsedOutput:
    def __init__(self, cover_letter_text, screening_drafts):
        self.cover_letter_text = cover_letter_text
        self.screening_drafts = [
            ScreeningDraft(question=q, answer=a) for q, a in screening_drafts
        ]


class _FakeResponse:
    def __init__(self, parsed_output=None, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.last_request = None

    def parse(self, **kwargs):
        self.last_request = kwargs
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self.messages = _FakeMessages(response=response, exc=exc)


def _profile(**overrides):
    fields = dict(
        id=1,
        full_name="Ada Lovelace",
        email="ada@example.com",
        target_roles=json.dumps(["Software Engineer"]),
        cover_letter_tone="professional",
    )
    fields.update(overrides)
    return JobsProfile(**fields)


def _company(session, name="Acme Corp"):
    row = JobCompany(name=name, source="watchlist")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _board(session, company_id):
    row = JobBoard(company_id=company_id, ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _posting(session, board_id, title="Backend Engineer"):
    row = JobPosting(
        board_id=board_id,
        external_id="1",
        title=title,
        apply_url="https://example.com/apply/1",
        comp_text="$150k-$180k",
        content_hash="abc",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _application(session, posting_id):
    row = JobApplication(posting_id=posting_id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# --- generate_packet ---


def test_generate_packet_success_returns_dict_and_empty_reason():
    response = _FakeResponse(
        parsed_output=_FakeParsedOutput(
            "Dear Hiring Manager, ...",
            [("Why this company?", "Because of the mission.")],
        )
    )
    client = _FakeClient(response=response)
    profile = _profile()
    posting = JobPosting(
        board_id=1,
        external_id="1",
        title="Backend Engineer",
        apply_url="https://example.com/apply/1",
        comp_text="$150k-$180k",
        content_hash="abc",
    )

    result, reason = generate_packet(profile, posting, "Acme Corp", client=client, model="fake-model")

    assert reason == ""
    assert result["cover_letter_text"] == "Dear Hiring Manager, ..."
    assert result["screening_drafts"] == [
        {"question": "Why this company?", "answer": "Because of the mission.", "source": "generated"}
    ]
    # Prompt used profile's textual fields + posting/company info, not resume_paths.
    sent = json.loads(client.messages.last_request["messages"][0]["content"])
    assert sent["profile"]["full_name"] == "Ada Lovelace"
    assert "resume_paths" not in sent["profile"]
    assert sent["posting"]["company"] == "Acme Corp"
    assert sent["posting"]["title"] == "Backend Engineer"


def test_generate_packet_degrades_on_unconfigured_client(monkeypatch):
    def _raise():
        raise TriageConfigError("ANTHROPIC_API_KEY not set")

    monkeypatch.setattr(packet, "make_client", _raise)
    profile = _profile()
    posting = JobPosting(
        board_id=1, external_id="1", title="X", apply_url="https://x", content_hash="h"
    )

    result, reason = generate_packet(profile, posting, "Acme Corp")

    assert result is None
    assert reason == "ANTHROPIC_API_KEY not set"


def test_generate_packet_degrades_on_refusal():
    client = _FakeClient(response=_FakeResponse(parsed_output=None, stop_reason="refusal"))
    profile = _profile()
    posting = JobPosting(
        board_id=1, external_id="1", title="X", apply_url="https://x", content_hash="h"
    )

    result, reason = generate_packet(profile, posting, "Acme Corp", client=client, model="fake-model")

    assert result is None
    assert reason == "stop_reason=refusal"


def test_generate_packet_degrades_on_sdk_exception():
    client = _FakeClient(exc=RuntimeError("boom"))
    profile = _profile()
    posting = JobPosting(
        board_id=1, external_id="1", title="X", apply_url="https://x", content_hash="h"
    )

    result, reason = generate_packet(profile, posting, "Acme Corp", client=client, model="fake-model")

    assert result is None
    assert reason == "RuntimeError"


# --- render_cover_letter_pdf ---


def test_render_cover_letter_pdf_writes_real_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(packet, "DB_PATH", tmp_path / "digest.db")

    out_path = render_cover_letter_pdf(42, "Dear Hiring Manager,\n\nI would love to join Acme.")

    written = Path(out_path)
    assert written == tmp_path / "jobs" / "applications" / "42" / "cover_letter.pdf"
    assert written.exists()
    with open(written, "rb") as fh:
        header = fh.read(5)
    assert header == b"%PDF-"


def test_render_cover_letter_pdf_overwrites_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(packet, "DB_PATH", tmp_path / "digest.db")

    first_path = render_cover_letter_pdf(7, "First draft.")
    second_path = render_cover_letter_pdf(7, "Second, longer, edited draft with more text.")

    assert first_path == second_path
    assert Path(second_path).exists()


# --- prepare_packet ---


def test_prepare_packet_success_stores_packet_json_and_pdf(session, tmp_path, monkeypatch):
    monkeypatch.setattr(packet, "DB_PATH", tmp_path / "digest.db")

    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)
    application = _application(session, posting.id)
    session.add(_profile())
    session.commit()

    response = _FakeResponse(
        parsed_output=_FakeParsedOutput(
            "Dear Hiring Manager, I am excited to apply.",
            [("Are you authorized to work?", "Yes.")],
        )
    )
    client = _FakeClient(response=response)

    ok, reason = prepare_packet(session, application.id, client=client, model="fake-model")

    assert ok is True
    assert reason == ""

    session.refresh(application)
    stored = json.loads(application.packet)
    assert stored["cover_letter_text"] == "Dear Hiring Manager, I am excited to apply."
    assert stored["screening_drafts"] == [
        {"question": "Are you authorized to work?", "answer": "Yes.", "source": "generated"}
    ]

    pdf_path = Path(application.cover_letter_path)
    assert pdf_path.exists()
    with open(pdf_path, "rb") as fh:
        assert fh.read(5) == b"%PDF-"


def test_prepare_packet_missing_application_fails_without_mutation(session):
    ok, reason = prepare_packet(session, 999)

    assert ok is False
    assert reason == "application not found"


def test_prepare_packet_missing_profile_fails_without_mutation(session, tmp_path, monkeypatch):
    monkeypatch.setattr(packet, "DB_PATH", tmp_path / "digest.db")

    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)
    application = _application(session, posting.id)
    # No JobsProfile row added.

    ok, reason = prepare_packet(session, application.id, client=_FakeClient())

    assert ok is False
    assert reason == "no profile configured"
    session.refresh(application)
    assert application.packet is None
    assert application.cover_letter_path is None


def test_prepare_packet_llm_degradation_leaves_application_untouched(session, tmp_path, monkeypatch):
    monkeypatch.setattr(packet, "DB_PATH", tmp_path / "digest.db")

    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)
    application = _application(session, posting.id)
    session.add(_profile())
    session.commit()

    client = _FakeClient(response=_FakeResponse(parsed_output=None, stop_reason="refusal"))

    ok, reason = prepare_packet(session, application.id, client=client, model="fake-model")

    assert ok is False
    assert reason == "stop_reason=refusal"
    session.refresh(application)
    assert application.packet is None
    assert application.cover_letter_path is None
