"""
Unit tests for pipeline.jobs.assist.mapping (T-027): deterministic profile rules, AnswerBank
reuse and the Claude fallback step of propose_values().

No network: the Claude client (a true external) is a scripted FakeClient, following
tests/test_domain_ideas.py's style. AnswerBank reuse hits a real temp sqlite db (monkeypatched
pipeline.db.engine), same fixture shape as tests/test_assist_client.py — nothing under pipeline/
is mocked. No Playwright: fields are plain dicts simulating extract_fields() output.
"""
import sys
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace

import anthropic
import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.db import AnswerBank, JobsProfile  # noqa: E402
from pipeline.jobs.assist import mapping  # noqa: E402
from pipeline.triage import TriageConfigError  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def make_profile(**overrides) -> JobsProfile:
    fields = {
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "555-1234",
        "linkedin_url": "https://linkedin.com/in/ada",
        "github_url": "https://github.com/ada",
        "portfolio_url": "https://ada.dev",
        "resume_paths": '["resume_abc.pdf"]',
        "eeo_answers": "{}",
    }
    fields.update(overrides)
    return JobsProfile(**fields)


def field(field_id: str, label: str, ftype: str = "text", **overrides) -> dict:
    base = {
        "field_id": field_id,
        "label": label,
        "type": ftype,
        "required": False,
        "options": [],
        "current_value": "",
        "section": "",
        "selector": f"#{field_id}",
    }
    base.update(overrides)
    return base


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


def generated_reply(*records: dict, stop_reason: str = "end_turn") -> SimpleNamespace:
    parsed = mapping._GeneratedBatch(fields=[mapping._GeneratedField(**record) for record in records])
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


# --- Deterministic profile rules -----------------------------------------------------------------


def test_maps_name_email_phone_from_profile():
    profile = make_profile()
    fields = [
        field("main:full_name", "Full Name"),
        field("main:email", "Email Address", ftype="email"),
        field("main:phone", "Mobile Phone"),
    ]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    by_id = {p.field_id: p for p in proposals}
    assert by_id["main:full_name"].value == "Ada Lovelace"
    assert by_id["main:full_name"].source == "profile"
    assert by_id["main:full_name"].needs_user is False
    assert by_id["main:email"].value == "ada@example.com"
    assert by_id["main:phone"].value == "555-1234"


def test_maps_linkedin_github_and_generic_url_to_portfolio():
    profile = make_profile()
    fields = [
        field("main:li", "LinkedIn Profile", ftype="url"),
        field("main:gh", "GitHub", ftype="url"),
        field("main:site", "Personal website", ftype="url"),
    ]
    proposals = {p.field_id: p for p in mapping.propose_values(fields, profile, client=FakeClient())}
    assert proposals["main:li"].value == "https://linkedin.com/in/ada"
    assert proposals["main:gh"].value == "https://github.com/ada"
    assert proposals["main:site"].value == "https://ada.dev"


def test_missing_profile_value_is_claimed_but_needs_user():
    profile = make_profile(phone=None)
    fields = [field("main:phone", "Phone Number")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert len(proposals) == 1
    assert proposals[0].value is None
    assert proposals[0].needs_user is True
    assert proposals[0].source == "profile"


def test_resume_upload_proposes_first_resume_path():
    profile = make_profile(resume_paths='["resume_a.pdf", "resume_b.pdf"]')
    fields = [field("main:resume", "Upload your resume/CV", ftype="file")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].value == "resume_a.pdf"
    assert proposals[0].source == "resume"
    assert proposals[0].needs_user is False


def test_resume_upload_with_no_resume_on_file_needs_user():
    profile = make_profile(resume_paths="[]")
    fields = [field("main:resume", "Resume", ftype="file")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].value is None
    assert proposals[0].needs_user is True
    assert proposals[0].source == "resume"


def test_cover_letter_upload_left_unmapped_but_not_needs_user():
    profile = make_profile()
    fields = [field("main:cl", "Cover Letter", ftype="file")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert len(proposals) == 1
    assert proposals[0].value is None
    assert proposals[0].needs_user is False
    assert proposals[0].source == "profile"


# --- Forced needs_user overrides ------------------------------------------------------------------


def test_attestation_field_forced_needs_user_even_with_answer_bank_hit(temp_db):
    profile = make_profile()
    with db.get_session() as session:
        session.add(AnswerBank(question_norm=mapping.normalize_title("I certify the above is true"),
                                question_raw="I certify the above is true", answer="Yes"))
        session.commit()
    fields = [field("main:cert", "I certify the above is true", ftype="checkbox")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].needs_user is True


def test_captcha_field_forced_needs_user():
    profile = make_profile()
    fields = [field("main:captcha", "Please complete the CAPTCHA")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].needs_user is True


# --- EEO fields ------------------------------------------------------------------------------------


def test_eeo_field_uses_stored_eeo_answer():
    profile = make_profile(eeo_answers='{"What is your gender?": "Prefer to self-describe: nonbinary"}')
    fields = [field("main:gender", "What is your gender?", ftype="select")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].value == "Prefer to self-describe: nonbinary"
    assert proposals[0].needs_user is False
    assert proposals[0].source == "profile"


def test_eeo_field_defaults_to_decline_when_no_stored_answer():
    profile = make_profile(eeo_answers="{}")
    fields = [field("main:veteran", "Veteran Status", ftype="select")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].value == "Decline to answer"
    assert proposals[0].needs_user is False


# --- AnswerBank reuse ------------------------------------------------------------------------------


def test_answer_bank_hit_used_for_screening_question(temp_db):
    profile = make_profile()
    with db.get_session() as session:
        session.add(
            AnswerBank(
                question_norm=mapping.normalize_title("Why do you want to work here?"),
                question_raw="Why do you want to work here?",
                answer="I admire the team's work on X.",
            )
        )
        session.commit()
    fields = [field("main:why", "Why do you want to work here?", ftype="textarea")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].value == "I admire the team's work on X."
    assert proposals[0].source == "answer_bank"
    assert proposals[0].needs_user is False


def test_answer_bank_picks_most_recently_updated_row(temp_db):
    profile = make_profile()
    norm = mapping.normalize_title("What are your salary expectations?")
    with db.get_session() as session:
        older = AnswerBank(question_norm=norm, question_raw="What are your salary expectations?", answer="Old answer")
        session.add(older)
        session.commit()
        session.refresh(older)
        newer = AnswerBank(question_norm=norm, question_raw="What are your salary expectations?", answer="New answer")
        session.add(newer)
        session.commit()
        session.refresh(newer)
        newer.updated_at = older.updated_at.replace(year=older.updated_at.year + 1)
        session.add(newer)
        session.commit()
    fields = [field("main:salary", "What are your salary expectations?")]
    proposals = mapping.propose_values(fields, profile, client=FakeClient())
    assert proposals[0].value == "New answer"


# --- Claude fallback for the rest -----------------------------------------------------------------


def test_blank_label_field_needs_user_without_model_call(temp_db):
    profile = make_profile()
    client = FakeClient()
    fields = [field("frame_0:mystery", "", ftype="text")]
    proposals = mapping.propose_values(fields, profile, client=client)
    assert proposals[0].needs_user is True
    assert proposals[0].source == "generated"
    assert client.calls == []


def test_remaining_fields_batched_to_one_model_call(temp_db):
    profile = make_profile()
    client = FakeClient(
        generated_reply(
            {"field_id": "main:q1", "value": "I thrive in ambiguity.", "needs_user": False},
            {"field_id": "main:q2", "value": None, "needs_user": True},
        )
    )
    fields = [
        field("main:q1", "Describe a time you overcame a challenge", ftype="textarea"),
        field("main:q2", "What is your desired equity range?", ftype="text"),
    ]
    proposals = {p.field_id: p for p in mapping.propose_values(fields, profile, client=client)}
    assert len(client.calls) == 1
    sent_payload = client.calls[0]["messages"][0]["content"]
    assert "main:q1" in sent_payload and "main:q2" in sent_payload
    assert client.calls[0]["output_format"] is mapping._GeneratedBatch
    assert proposals["main:q1"].value == "I thrive in ambiguity."
    assert proposals["main:q1"].source == "generated"
    assert proposals["main:q1"].needs_user is False
    assert proposals["main:q2"].needs_user is True


def test_model_reply_never_leaks_a_field_id_it_was_not_asked_about(temp_db):
    profile = make_profile()
    client = FakeClient(
        generated_reply(
            {"field_id": "main:q1", "value": "answer", "needs_user": False},
            {"field_id": "main:not-asked", "value": "sneaky", "needs_user": False},
        )
    )
    fields = [field("main:q1", "Tell us about yourself", ftype="textarea")]
    proposals = mapping.propose_values(fields, profile, client=client)
    assert [p.field_id for p in proposals] == ["main:q1"]


def test_generate_step_refusal_degrades_to_needs_user(temp_db):
    profile = make_profile()
    client = FakeClient(generated_reply(stop_reason="refusal"))
    fields = [field("main:q1", "Tell us about yourself", ftype="textarea")]
    proposals = mapping.propose_values(fields, profile, client=client)
    assert proposals[0].needs_user is True
    assert proposals[0].source == "generated"
    assert proposals[0].confidence == 0.0


def test_generate_step_api_error_degrades_to_needs_user(temp_db):
    profile = make_profile()
    client = FakeClient(connection_error())
    fields = [field("main:q1", "Tell us about yourself", ftype="textarea")]
    proposals = mapping.propose_values(fields, profile, client=client)
    assert proposals[0].needs_user is True


def test_unconfigured_api_key_never_raises(monkeypatch, temp_db):
    profile = make_profile()

    def fake_make_client() -> Any:
        raise TriageConfigError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(mapping, "make_client", fake_make_client)
    fields = [field("main:q1", "Tell us about yourself", ftype="textarea")]
    proposals = mapping.propose_values(fields, profile)
    assert proposals[0].needs_user is True
    assert proposals[0].source == "generated"


def test_model_never_receives_current_value_section_or_selector(temp_db):
    """Prompt-injection guardrail: only field_id/label/type/options reach the model request."""
    profile = make_profile()
    client = FakeClient(generated_reply({"field_id": "main:q1", "value": "x", "needs_user": False}))
    fields = [
        field(
            "main:q1",
            "Tell us about yourself",
            ftype="textarea",
            current_value="<script>steal()</script>",
            section="secret section",
            selector="#do-not-leak",
        )
    ]
    mapping.propose_values(fields, profile, client=client)
    sent_payload = client.calls[0]["messages"][0]["content"]
    assert "do-not-leak" not in sent_payload
    assert "secret section" not in sent_payload
    assert "steal" not in sent_payload
