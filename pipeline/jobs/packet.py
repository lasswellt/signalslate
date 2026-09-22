"""
Apply-packet generation: a Claude-drafted cover letter + screening-answer drafts, plus the fpdf2
cover-letter PDF the checklist overlay/desktop runner can hand to the applicant (research doc
"Apply Assist" -> "Cover letter and resume").

Design decisions:

- generate_packet() mirrors pipeline.domains.ideas.llm_ideas()'s structured-output +
  graceful-degradation pattern exactly: make_client()/llm_settings()["map_model"], a pydantic
  extra="forbid" response model, client.messages.parse(output_format=...), a (result, reason)
  return tuple, and an unconfigured key / refusal / parse or SDK failure all degrade to
  (None, reason) rather than raising — a failed draft should not crash prepare_packet(), it should
  leave the application untouched so the user can retry.
- JobsProfile.resume_paths is a JSON list of file PATHS (e.g. "resume.pdf" on disk), not resume
  text. This task does not read/parse the file at those paths — that "extract the actual resume
  text and feed it to the model" integration is out of scope here and left as a documented gap;
  the prompt instead uses the profile's own textual fields (target_roles, cover_letter_tone,
  contact info) plus the posting/company info. A later task that wires real resume-text extraction
  can pass richer content into generate_packet() without changing its signature.
- ScreeningDraft.source is a fixed "generated" literal for every draft this module produces: there
  is no answer-bank/profile-match step feeding this call yet, so "profile"/"answer_bank" values
  simply don't arise here. If a later task adds that matching step and calls into this module (or
  reuses ScreeningDraft), it can populate source with those richer values; this task's own model
  calls always stamp "generated".
- render_cover_letter_pdf() is a deliberately plain fpdf2 text render (Helvetica, multi_cell word
  wrap) — no letterhead/layout — per the task notes ("keep it simple ... no fancy layout"). It
  writes to a fixed per-application path and overwrites on every call, matching the task's "editing
  the text and re-rendering" note; no versioning.
"""
import json
import logging
from typing import Any, Optional

import anthropic
from fpdf import FPDF
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlmodel import Session

from pipeline.db import DB_PATH, JobApplication, JobBoard, JobCompany, JobPosting, JobsProfile
from pipeline.health import llm_settings
from pipeline.triage import TriageConfigError, make_client

_log = logging.getLogger(__name__)

_PACKET_MAX_TOKENS = 2048
_PACKET_SYSTEM_PROMPT = """\
You draft job-application materials for a candidate applying to a specific posting.
Write one tailored cover letter (a few short paragraphs, no placeholders like "[Company Name]")
in the requested tone, and draft answers for any screening questions the candidate has flagged.
Return only the requested structured fields, no markdown, no extra commentary."""


class ScreeningDraft(BaseModel):
    """One drafted screening-question answer. `source` is always "generated" for a call made
    through this module (see module docstring); richer values are reserved for a future
    answer-bank/profile-match step."""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    source: str = "generated"


class _ApplyPacket(BaseModel):
    """Structured response shape for generate_packet()'s client.messages.parse() call."""

    model_config = ConfigDict(extra="forbid")

    cover_letter_text: str
    screening_drafts: list[ScreeningDraft]


def _profile_prompt_fields(profile: JobsProfile) -> dict[str, Any]:
    """The subset of JobsProfile that is plain text/contact info suitable for prompting — never
    the resume file paths themselves (see module docstring: this task doesn't read resume text)."""
    try:
        target_roles = json.loads(profile.target_roles or "[]")
    except (json.JSONDecodeError, TypeError):
        target_roles = []
    return {
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "linkedin_url": profile.linkedin_url,
        "github_url": profile.github_url,
        "portfolio_url": profile.portfolio_url,
        "target_roles": target_roles,
        "cover_letter_tone": profile.cover_letter_tone,
    }


def generate_packet(
    profile: JobsProfile,
    posting: JobPosting,
    company_name: str,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """
    Drafts a cover letter and screening-answer stubs for one posting via the Anthropic client.

    Reuses pipeline.triage.make_client()/pipeline.health.llm_settings() rather than a second LLM
    wiring path, mirroring pipeline.domains.ideas.llm_ideas(). The prompt is built from `profile`'s
    textual fields (contact info, target_roles, cover_letter_tone — NOT resume_paths, which are
    file paths this task does not read) plus `posting`'s title/comp_text/apply_url and
    `company_name`.

    Args:
        profile: The user's JobsProfile row.
        posting: The JobPosting being applied to.
        company_name: The resolved company name for the posting's board.
        client: An anthropic.Anthropic-like object (duck-typed: messages.parse(**kwargs) returns
            an object with parsed_output/stop_reason). Tests inject a fake here instead of hitting
            the network. Defaults to pipeline.triage.make_client().
        model: Overrides llm_settings()["map_model"]; tests pass a fixed model id.

    Returns:
        (result, reason). On success, result is {"cover_letter_text": str, "screening_drafts":
        [{"question", "answer", "source"}, ...]} and reason is "". On any failure — unconfigured
        key, a refusal, or the failing exception's CLASS name (never its message) — result is None
        and reason is a short, non-sensitive explanation. Never raises for a configuration or
        model/API failure.
    """
    active_client = client
    if active_client is None:
        try:
            active_client = make_client()
        except TriageConfigError as exc:
            return None, str(exc)

    active_model = model if model is not None else llm_settings()["map_model"]
    request_payload = {
        "profile": _profile_prompt_fields(profile),
        "posting": {
            "title": posting.title,
            "company": company_name,
            "comp_text": posting.comp_text,
            "apply_url": posting.apply_url,
        },
    }
    request = {
        "model": active_model,
        "max_tokens": _PACKET_MAX_TOKENS,
        "system": _PACKET_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(request_payload, ensure_ascii=False)}],
        "output_format": _ApplyPacket,
    }

    try:
        response = active_client.messages.parse(**request)
    except (anthropic.APIError, ValidationError) as exc:
        return None, type(exc).__name__
    except Exception as exc:
        # Class name only: an arbitrary exception here can echo the request or response text.
        return None, type(exc).__name__

    if response.stop_reason in ("refusal", "max_tokens"):
        return None, f"stop_reason={response.stop_reason}"
    if response.parsed_output is None:
        return None, "no parsed output"

    parsed = response.parsed_output
    return (
        {
            "cover_letter_text": parsed.cover_letter_text,
            "screening_drafts": [draft.model_dump() for draft in parsed.screening_drafts],
        },
        "",
    )


def render_cover_letter_pdf(application_id: int, text: str) -> str:
    """
    Renders `text` as a plain single-column PDF and writes it to
    DB_PATH.parent / "jobs" / "applications" / str(application_id) / "cover_letter.pdf".

    Overwrites any existing file at that path — no versioning: re-rendering after the user edits
    the drafted text is expected (task notes), not an error case.

    Args:
        application_id: The JobApplication's id; scopes the output path.
        text: The cover letter body to render.

    Returns:
        The written PDF's path, as a string.
    """
    out_dir = DB_PATH.parent / "jobs" / "applications" / str(application_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cover_letter.pdf"

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text)
    pdf.output(str(out_path))
    return str(out_path)


def prepare_packet(
    session: Session,
    application_id: int,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Loads one JobApplication and its posting/company/profile, drafts an apply packet, stores it
    and renders the cover-letter PDF.

    On success, JSON-serializes {"cover_letter_text", "screening_drafts"} into
    `application.packet`, writes the PDF via render_cover_letter_pdf() and sets
    `application.cover_letter_path`, then commits. On any failure — application/posting/board/
    company not found, no JobsProfile configured, or generate_packet() degrading — the application
    row is left untouched and (False, reason) is returned. Never raises.

    Args:
        session: An open SQLModel Session.
        application_id: The JobApplication.id to prepare a packet for.
        client: Forwarded to generate_packet(); tests inject a fake instead of hitting the network.
        model: Forwarded to generate_packet().

    Returns:
        (True, "") on success; (False, reason) on any failure, without mutating the application.
    """
    application = session.get(JobApplication, application_id)
    if application is None:
        return False, "application not found"

    posting = session.get(JobPosting, application.posting_id)
    if posting is None:
        return False, "posting not found"

    board = session.get(JobBoard, posting.board_id)
    if board is None:
        return False, "board not found"

    company = session.get(JobCompany, board.company_id)
    if company is None:
        return False, "company not found"

    profile = session.get(JobsProfile, 1)
    if profile is None:
        return False, "no profile configured"

    result, reason = generate_packet(profile, posting, company.name, client=client, model=model)
    if result is None:
        return False, reason

    application.packet = json.dumps(result, ensure_ascii=False)
    application.cover_letter_path = render_cover_letter_pdf(application_id, result["cover_letter_text"])
    session.add(application)
    session.commit()
    return True, ""
