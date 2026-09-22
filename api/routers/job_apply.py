"""
Apply-assist routes: the user's own JobsProfile, resume upload, the reusable AnswerBank, and
JobApplication create/list/status/packet (T-023), plus the assist queue/claim/progress/file-download
routes the desktop runner polls (T-024; docs/_research/2026-09-21_jobs-collector.md "Apply Assist"
§Where it runs). Mirrors api/routers/domain_buy.py's structure.

Design decisions:

- GET /jobs/profile lazily creates the id=1 singleton row on first read rather than 404ing: "the
  app always has exactly one profile" (pipeline.db.JobsProfile docstring), so there is never a
  meaningful "no profile" state for this route to report — only an empty one.
- JobsProfileUpdate mirrors JobsProfile's fields except id (server-assigned), updated_at
  (server-stamped) and resume_paths: resumes are appended only by POST /jobs/profile/resume, so a
  PUT full-replace can never silently drop a previously uploaded resume path.
- Resume upload is JSON {filename, content_b64} (api/security.py: bodies are JSON only). The
  client-supplied filename is never used to build a path (path-traversal risk) or trusted for
  format (a renamed .pdf could be anything) — the stored name is always `resume_<uuid4 hex>.pdf`
  under DB_PATH.parent / "jobs" / "resumes", and the PDF check is on the decoded bytes' magic
  number, not the extension. DB_PATH is imported by name (not looked up through pipeline.db each
  call) so tests can monkeypatch job_apply.DB_PATH the same way tests/test_jobs_packet.py
  monkeypatches packet.DB_PATH.
- AnswerBank.question_norm reuses pipeline.jobs.normalize_title's lowercase/fold-punctuation
  normalization (imported by name, like domain_buy imports normalize_domain — a pure utility
  function, not a mutation module) rather than inventing a second normalizer; GET /jobs/answers's
  `q` filter normalizes the same way before the substring match so casing/punctuation in the query
  cannot silently miss a stored answer. POST always inserts (AnswerBank is not deduped by design,
  per its own docstring).
- POST /jobs/applications checks posting existence and the UNIQUE posting_id constraint explicitly
  (404 / 409) rather than letting a raw IntegrityError surface, matching this codebase's
  coded-error convention everywhere else.
- PATCH /jobs/applications/{id} defers all legality of the move to pipeline.jobs.apply.transition;
  the body's `status` is a plain str (not a Literal) precisely so an illegal or unknown value
  reaches transition() and comes back as ValueError -> 422 illegal_transition, rather than a bare
  Pydantic 422 that never names the reason. Reaching "submitted" also stamps submitted_at — the
  only place in this codebase that can set JobApplication.status to "submitted" per apply.py's own
  docstring ("the runner/scheduler must never call this with to_status=submitted on its own").
- POST /jobs/applications/{id}/packet checks the application exists itself before calling
  packet.prepare_packet(), so a missing application is reported as 404 application_not_found
  rather than the 422 packet_failed prepare_packet() would otherwise return for the same case
  (prepare_packet's own "application not found" reason is meant for callers that already know the
  row might not exist, not restated as this route's primary 404 signal). Every other prepare_packet
  failure reason is already a short, safe string (see its own docstring) and is returned verbatim.
- PUT /jobs/applications/{id}/packet edits only cover_letter_text in the stored packet JSON,
  leaving screening_drafts untouched (or defaulting to an empty list if no packet has been prepared
  yet), then re-renders the PDF via packet.render_cover_letter_pdf() and updates
  cover_letter_path — "editing the text re-renders" per the task notes, with no versioning, same as
  render_cover_letter_pdf()'s own contract.
- pipeline.jobs.apply and pipeline.jobs.packet are imported by module, not by name, so tests can
  monkeypatch at this file's own import site (job_apply.apply.transition,
  job_apply.packet.prepare_packet/make_client), mirroring api/routers/jobs.py's convention for
  pipeline.jobs.seeds/resolve/research/ats.
- Every handler here is plain `def`: all DB access and packet.prepare_packet()'s LLM call are
  blocking I/O, matching every other router in this codebase.
- POST /jobs/applications/{id}/assist only queues from status == "ready" (the task notes'
  "only from ready" — the application's overall `status`, not `assist_state`): a 422 not_ready
  otherwise. It also refuses to re-queue an application whose assist_state is already
  queued/claimed/running (409 assist_already_active), so a second click can't spawn two runner
  sessions for the same application; queued/idle/paused/done/failed all remain fine ready-state
  applications to (re)queue.
- GET /jobs/assist/queue returns the single oldest queued application, ordered by ascending `id`:
  JobApplication has no dedicated "queued_at" column (T-016's schema), and ids only increase, so
  ascending id is a stable proxy for "oldest queued" without a migration. Returns null (not 404)
  when nothing is queued — polling an empty queue is the expected steady state, not an error.
- POST /jobs/assist/queue/{id}/claim does its queued -> claimed check-then-set inside one
  db.get_session() transaction (read the row, verify assist_state == "queued", flip it, commit) —
  never trusting a value read in an earlier request/session — so two concurrent claims on the same
  row can't both succeed; the loser gets 409 not_queued.
- PATCH /jobs/assist/sessions/{session_id} looks the application up by assist_session_id, never by
  application id, since it's the runner's session token, not something the runner necessarily
  tracks the application row for. assist_state is validated against the same idle/queued/claimed/
  running/paused/done/failed set as pipeline.db.JobApplication's own comment (422
  unknown_assist_state otherwise); there is deliberately no `status` field on this body at all, so
  this route can never reach JobApplication.status, let alone "submitted" (pipeline.jobs.apply's own
  contract — see PATCH /jobs/applications/{id} above). filled_fields are appended to the assist_log
  JSON array (loading, extending, re-serializing — never replacing it), tagged with `step` when
  given, keeping the full audit trail the research doc's "Data model additions" describes.
  approved_answers reuse _insert_answer(), the same insert path POST /jobs/answers uses, so the
  normalization is defined once.
- GET /jobs/applications/{id}/files/{kind} mirrors runs.py's two-stage check (path unset -> 404,
  path set but missing on disk -> 404) before FileResponse, rather than trusting the DB column alone.
- POST /jobs/assist/sessions/{session_id}/propose (T-030) is the ONLY place the desktop runner's
  field list meets pipeline.jobs.assist.mapping.propose_values(), and therefore the only place
  ANTHROPIC_API_KEY is used in the whole apply-assist flow: the runner posts the plain field
  descriptors it extracted in-page and gets proposals back, so the key never leaves the server (see
  mapping.py's own "runs SERVER-side" docstring). mapping is imported by module, like apply/packet
  above, so tests monkeypatch job_apply.mapping.make_client at this file's import site. The route
  looks the application up by assist_session_id (the same lookup PATCH
  /jobs/assist/sessions/{session_id} uses — the runner holds a session id, not necessarily the
  application row) and passes the singleton JobsProfile straight through; body.fields stays
  list[dict] rather than a typed FieldDescriptor model because extract.js's field shape is the
  desktop extractor's contract, not this API's, and propose_values() already ignores every key it
  doesn't use.
"""
import base64
import binascii
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from api.serialize import iso_z
from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import DB_PATH, AnswerBank, JobApplication, JobPosting, JobsProfile
from pipeline.jobs import apply, normalize_title, packet
from pipeline.jobs.assist import mapping

router = APIRouter(tags=["job_apply"])

_log = logging.getLogger(__name__)

_RESUME_MAX_BYTES = 5 * 1024 * 1024
_RESUME_MAX_B64_CHARS = 7_000_000  # ~5 MB decoded, base64-inflated, before ever calling b64decode
_PDF_MAGIC = b"%PDF-"

_MAX_TEXT_CHARS = 5000
_MAX_FILENAME_CHARS = 255
_MAX_LIST_ITEMS = 50
# A single Workday/Greenhouse page can carry far more fillable inputs than _MAX_LIST_ITEMS allows
# for the small progress-report lists, so the /propose body gets its own, roomier cap.
_MAX_PROPOSE_FIELDS = 300


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _json_list(value: Optional[str]) -> list[Any]:
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


def _json_dict(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


# --- JobsProfile ------------------------------------------------------------------------------


class JobsProfileOut(BaseModel):
    id: int
    full_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    linkedin_url: Optional[str]
    github_url: Optional[str]
    portfolio_url: Optional[str]
    other_links: list[Any]
    work_authorized: Optional[bool]
    needs_sponsorship: Optional[bool]
    open_to_relocation: Optional[bool]
    relocation_notes: Optional[str]
    start_date_notes: Optional[str]
    salary_floor: Optional[str]
    salary_disclosure_policy: str
    eeo_answers: dict[str, Any]
    target_roles: list[str]
    target_locations: list[str]
    target_remote: Optional[bool]
    target_salary_floor: Optional[str]
    target_exclusions: list[str]
    resume_paths: list[str]
    cover_letter_tone: Optional[str]
    updated_at: Optional[str]


class JobsProfileUpdate(BaseModel):
    """Full-replace body for PUT /jobs/profile — every JobsProfile field except id, updated_at
    (server-managed) and resume_paths (managed only by POST /jobs/profile/resume; see module
    docstring)."""

    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    email: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    phone: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    linkedin_url: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    github_url: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    portfolio_url: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    other_links: list[Any] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    work_authorized: Optional[bool] = None
    needs_sponsorship: Optional[bool] = None
    open_to_relocation: Optional[bool] = None
    relocation_notes: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    start_date_notes: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    salary_floor: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    salary_disclosure_policy: str = "decline"
    eeo_answers: dict[str, Any] = Field(default_factory=dict)
    target_roles: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    target_locations: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    target_remote: Optional[bool] = None
    target_salary_floor: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    target_exclusions: list[str] = Field(default_factory=list, max_length=_MAX_LIST_ITEMS)
    cover_letter_tone: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)


class ResumeUploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(max_length=_MAX_FILENAME_CHARS)
    content_b64: str = Field(max_length=_RESUME_MAX_B64_CHARS)


class ResumeUploadOut(BaseModel):
    resume_paths: list[str]


def _profile_out(row: JobsProfile) -> JobsProfileOut:
    assert row.id is not None  # row came from a select()/get() or a just-committed insert
    return JobsProfileOut(
        id=row.id,
        full_name=row.full_name,
        email=row.email,
        phone=row.phone,
        linkedin_url=row.linkedin_url,
        github_url=row.github_url,
        portfolio_url=row.portfolio_url,
        other_links=_json_list(row.other_links),
        work_authorized=row.work_authorized,
        needs_sponsorship=row.needs_sponsorship,
        open_to_relocation=row.open_to_relocation,
        relocation_notes=row.relocation_notes,
        start_date_notes=row.start_date_notes,
        salary_floor=row.salary_floor,
        salary_disclosure_policy=row.salary_disclosure_policy,
        eeo_answers=_json_dict(row.eeo_answers),
        target_roles=_json_list(row.target_roles),
        target_locations=_json_list(row.target_locations),
        target_remote=row.target_remote,
        target_salary_floor=row.target_salary_floor,
        target_exclusions=_json_list(row.target_exclusions),
        resume_paths=_json_list(row.resume_paths),
        cover_letter_tone=row.cover_letter_tone,
        updated_at=iso_z(row.updated_at),
    )


def _get_or_create_profile(session) -> JobsProfile:
    row = session.get(JobsProfile, 1)
    if row is None:
        row = JobsProfile(id=1)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


@router.get("/jobs/profile", response_model=JobsProfileOut)
def get_profile() -> JobsProfileOut:
    """The singleton profile, lazily created empty on first read (see module docstring)."""
    with db.get_session() as session:
        return _profile_out(_get_or_create_profile(session))


@router.put("/jobs/profile", response_model=JobsProfileOut)
def update_profile(body: JobsProfileUpdate) -> JobsProfileOut:
    """Full-replace update of the singleton profile (resume_paths excluded; see module docstring)."""
    with db.get_session() as session:
        row = _get_or_create_profile(session)
        row.full_name = body.full_name
        row.email = body.email
        row.phone = body.phone
        row.linkedin_url = body.linkedin_url
        row.github_url = body.github_url
        row.portfolio_url = body.portfolio_url
        row.other_links = json.dumps(body.other_links, ensure_ascii=False)
        row.work_authorized = body.work_authorized
        row.needs_sponsorship = body.needs_sponsorship
        row.open_to_relocation = body.open_to_relocation
        row.relocation_notes = body.relocation_notes
        row.start_date_notes = body.start_date_notes
        row.salary_floor = body.salary_floor
        row.salary_disclosure_policy = body.salary_disclosure_policy
        row.eeo_answers = json.dumps(body.eeo_answers, ensure_ascii=False)
        row.target_roles = json.dumps(body.target_roles, ensure_ascii=False)
        row.target_locations = json.dumps(body.target_locations, ensure_ascii=False)
        row.target_remote = body.target_remote
        row.target_salary_floor = body.target_salary_floor
        row.target_exclusions = json.dumps(body.target_exclusions, ensure_ascii=False)
        row.cover_letter_tone = body.cover_letter_tone
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _profile_out(row)


@router.post("/jobs/profile/resume", response_model=ResumeUploadOut)
def upload_resume(body: ResumeUploadBody) -> ResumeUploadOut:
    """Decodes, validates (size + real PDF magic number) and stores one resume PDF under a
    generated filename, appending its path to the profile's resume_paths. Never echoes bytes."""
    try:
        content = base64.b64decode(body.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise _coded(422, "invalid_base64", "content_b64 could not be decoded") from None
    if len(content) > _RESUME_MAX_BYTES:
        raise _coded(422, "resume_too_large", f"resume exceeds {_RESUME_MAX_BYTES} bytes")
    if not content.startswith(_PDF_MAGIC):
        raise _coded(422, "not_a_pdf", "resume must be a PDF file")

    resumes_dir = DB_PATH.parent / "jobs" / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    stored_path = resumes_dir / f"resume_{uuid.uuid4().hex}.pdf"
    stored_path.write_bytes(content)

    with db.get_session() as session:
        row = _get_or_create_profile(session)
        paths = _json_list(row.resume_paths)
        paths.append(str(stored_path))
        row.resume_paths = json.dumps(paths, ensure_ascii=False)
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return ResumeUploadOut(resume_paths=_json_list(row.resume_paths))


# --- AnswerBank -------------------------------------------------------------------------------


class AnswerCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_raw: str = Field(max_length=_MAX_TEXT_CHARS)
    answer: str = Field(max_length=_MAX_TEXT_CHARS)


class AnswerOut(BaseModel):
    id: int
    question_norm: str
    question_raw: str
    answer: str
    source_application_id: Optional[int]
    updated_at: Optional[str]


def _answer_out(row: AnswerBank) -> AnswerOut:
    assert row.id is not None  # row came from a select() or a just-committed insert
    return AnswerOut(
        id=row.id,
        question_norm=row.question_norm,
        question_raw=row.question_raw,
        answer=row.answer,
        source_application_id=row.source_application_id,
        updated_at=iso_z(row.updated_at),
    )


@router.get("/jobs/answers", response_model=list[AnswerOut])
def list_answers(q: Optional[str] = None) -> list[AnswerOut]:
    """Every stored answer, newest first, optionally filtered to question_norm containing
    normalize_title(q) (see module docstring for why q is normalized the same way)."""
    with db.get_session() as session:
        statement = select(AnswerBank)
        if q:
            statement = statement.where(col(AnswerBank.question_norm).contains(normalize_title(q)))
        rows = session.exec(statement.order_by(col(AnswerBank.updated_at).desc())).all()
        return [_answer_out(row) for row in rows]


def _insert_answer(
    session, question_raw: str, answer: str, source_application_id: Optional[int] = None
) -> AnswerBank:
    """Shared insert path for AnswerBank rows: POST /jobs/answers and PATCH
    /jobs/assist/sessions/{session_id}'s approved_answers both go through here so the
    normalize_title normalization is defined once. Always inserts — AnswerBank is not deduped by
    design (see its own docstring)."""
    row = AnswerBank(
        question_norm=normalize_title(question_raw),
        question_raw=question_raw,
        answer=answer,
        source_application_id=source_application_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.post("/jobs/answers", status_code=201, response_model=AnswerOut)
def create_answer(body: AnswerCreateBody) -> AnswerOut:
    """Always inserts a new row — AnswerBank is not deduped by design (see its own docstring)."""
    with db.get_session() as session:
        row = _insert_answer(session, body.question_raw, body.answer)
        return _answer_out(row)


# --- JobApplication ----------------------------------------------------------------------------

_APPLICATION_STATUSES = Literal[
    "saved", "preparing", "ready", "submitted", "interviewing", "rejected", "closed", "withdrawn"
]


class ApplicationCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posting_id: int


class ApplicationStatusBody(BaseModel):
    """status is a plain str, not a Literal — an illegal/unknown value must reach
    pipeline.jobs.apply.transition() so its ValueError becomes this route's 422 illegal_transition
    (see module docstring)."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(max_length=_MAX_TEXT_CHARS)


class PacketEditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cover_letter_text: str = Field(max_length=_MAX_TEXT_CHARS)


class ApplicationOut(BaseModel):
    id: int
    posting_id: int
    status: str
    packet: Optional[dict[str, Any]]
    cover_letter_path: Optional[str]
    resume_path: Optional[str]
    assist_state: str
    assist_session_id: Optional[str]
    created_at: Optional[str]
    submitted_at: Optional[str]


def _application_out(row: JobApplication) -> ApplicationOut:
    assert row.id is not None  # row came from a select()/get() or a just-committed insert
    return ApplicationOut(
        id=row.id,
        posting_id=row.posting_id,
        status=row.status,
        packet=json.loads(row.packet) if row.packet else None,
        cover_letter_path=row.cover_letter_path,
        resume_path=row.resume_path,
        assist_state=row.assist_state,
        assist_session_id=row.assist_session_id,
        created_at=iso_z(row.created_at),
        submitted_at=iso_z(row.submitted_at),
    )


@router.get("/jobs/applications", response_model=list[ApplicationOut])
def list_applications(status: Optional[_APPLICATION_STATUSES] = None) -> list[ApplicationOut]:
    """Every application, newest first, optionally filtered to one of the 8 canonical statuses."""
    with db.get_session() as session:
        statement = select(JobApplication)
        if status is not None:
            statement = statement.where(JobApplication.status == status)
        rows = session.exec(statement.order_by(col(JobApplication.created_at).desc())).all()
        return [_application_out(row) for row in rows]


@router.post("/jobs/applications", status_code=201, response_model=ApplicationOut)
def create_application(body: ApplicationCreateBody) -> ApplicationOut:
    """Creates a new application against one posting. 404 if the posting doesn't exist, 409 if an
    application already exists for it (posting_id is UNIQUE)."""
    with db.get_session() as session:
        posting = session.get(JobPosting, body.posting_id)
        if posting is None:
            raise _coded(404, "posting_not_found", "Posting not found")
        existing = session.exec(
            select(JobApplication).where(JobApplication.posting_id == body.posting_id)
        ).first()
        if existing is not None:
            raise _coded(409, "application_exists", "An application already exists for this posting")
        row = JobApplication(posting_id=body.posting_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _application_out(row)


@router.patch("/jobs/applications/{id}", response_model=ApplicationOut)
def update_application_status(id: int, body: ApplicationStatusBody) -> ApplicationOut:
    """Moves an application's status through apply.transition(); an illegal move is 422
    illegal_transition, never a silent no-op. Reaching "submitted" stamps submitted_at."""
    with db.get_session() as session:
        row = session.get(JobApplication, id)
        if row is None:
            raise _coded(404, "application_not_found", "Application not found")
        try:
            apply.transition(row, body.status)
        except ValueError as exc:
            raise _coded(422, "illegal_transition", str(exc)) from None
        if row.status == "submitted":
            row.submitted_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _application_out(row)


@router.post("/jobs/applications/{id}/packet", response_model=ApplicationOut)
def prepare_application_packet(id: int) -> ApplicationOut:
    """Drafts the cover letter + screening-answer packet via packet.prepare_packet(). 404 if the
    application itself doesn't exist (checked here, ahead of prepare_packet's own lookup — see
    module docstring); any other prepare_packet failure is 422 with its own reason string."""
    with db.get_session() as session:
        row = session.get(JobApplication, id)
        if row is None:
            raise _coded(404, "application_not_found", "Application not found")
        ok, reason = packet.prepare_packet(session, id)
        if not ok:
            raise _coded(422, "packet_failed", reason)
        session.refresh(row)
        return _application_out(row)


@router.put("/jobs/applications/{id}/packet", response_model=ApplicationOut)
def edit_application_packet(id: int, body: PacketEditBody) -> ApplicationOut:
    """Edits the drafted cover_letter_text and re-renders the PDF (see module docstring)."""
    with db.get_session() as session:
        row = session.get(JobApplication, id)
        if row is None:
            raise _coded(404, "application_not_found", "Application not found")
        packet_data = json.loads(row.packet) if row.packet else {"screening_drafts": []}
        packet_data["cover_letter_text"] = body.cover_letter_text
        row.packet = json.dumps(packet_data, ensure_ascii=False)
        row.cover_letter_path = packet.render_cover_letter_pdf(id, body.cover_letter_text)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _application_out(row)


# --- Apply assist: queue / claim / progress / files (T-024) ---------------------------------------

_ASSIST_STATES = frozenset({"idle", "queued", "claimed", "running", "paused", "done", "failed"})
_ASSIST_ACTIVE_STATES = frozenset({"queued", "claimed", "running"})

_FILE_PATH_FIELDS = {"cover_letter": "cover_letter_path", "resume": "resume_path"}


class FilledFieldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(max_length=_MAX_TEXT_CHARS)
    value: str = Field(max_length=_MAX_TEXT_CHARS)
    source: str = Field(max_length=_MAX_TEXT_CHARS)
    confidence: float = Field(ge=0.0, le=1.0)


class ApprovedAnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=_MAX_TEXT_CHARS)
    answer: str = Field(max_length=_MAX_TEXT_CHARS)


class AssistSessionUpdateBody(BaseModel):
    """PATCH body for the desktop runner's progress reports (see module docstring). assist_state is
    a plain str, validated against _ASSIST_STATES inside the handler (422 unknown_assist_state)
    rather than a Literal, mirroring ApplicationStatusBody's own "reach the handler, name the
    reason" convention. There is deliberately no `status` field: this body can never touch
    JobApplication.status."""

    model_config = ConfigDict(extra="forbid")

    step: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    assist_state: Optional[str] = Field(default=None, max_length=_MAX_TEXT_CHARS)
    filled_fields: Optional[list[FilledFieldBody]] = Field(default=None, max_length=_MAX_LIST_ITEMS)
    approved_answers: Optional[list[ApprovedAnswerBody]] = Field(default=None, max_length=_MAX_LIST_ITEMS)


@router.post("/jobs/applications/{id}/assist", response_model=ApplicationOut)
def queue_assist(id: int) -> ApplicationOut:
    """Queues an assist session for this application: legal only when status == "ready" (422
    not_ready otherwise; see module docstring for why status, not assist_state, gates this). Also
    refuses to queue on top of an already-active session (409 assist_already_active)."""
    with db.get_session() as session:
        row = session.get(JobApplication, id)
        if row is None:
            raise _coded(404, "application_not_found", "Application not found")
        if row.status != "ready":
            raise _coded(422, "not_ready", "Application status must be 'ready' to queue assist")
        if row.assist_state in _ASSIST_ACTIVE_STATES:
            raise _coded(409, "assist_already_active", "An assist session is already active for this application")
        row.assist_state = "queued"
        session.add(row)
        session.commit()
        session.refresh(row)
        return _application_out(row)


@router.get("/jobs/assist/queue", response_model=Optional[ApplicationOut])
def get_assist_queue_head() -> Optional[ApplicationOut]:
    """The next application for the desktop runner to claim: the oldest queued row by ascending id
    (see module docstring). Null, not 404, when nothing is queued."""
    with db.get_session() as session:
        row = session.exec(
            select(JobApplication)
            .where(JobApplication.assist_state == "queued")
            .order_by(col(JobApplication.id).asc())
        ).first()
        if row is None:
            return None
        return _application_out(row)


@router.post("/jobs/assist/queue/{id}/claim", response_model=ApplicationOut)
def claim_assist(id: int) -> ApplicationOut:
    """Atomic queued -> claimed transition with a freshly generated assist_session_id (see module
    docstring for why the check-then-set happens inside one transaction). 409 not_queued if the
    application isn't currently queued (already claimed by another poll, or never queued)."""
    with db.get_session() as session:
        row = session.get(JobApplication, id)
        if row is None:
            raise _coded(404, "application_not_found", "Application not found")
        if row.assist_state != "queued":
            raise _coded(409, "not_queued", "Application is not currently queued for assist")
        row.assist_state = "claimed"
        row.assist_session_id = uuid.uuid4().hex
        session.add(row)
        session.commit()
        session.refresh(row)
        return _application_out(row)


@router.patch("/jobs/assist/sessions/{session_id}", response_model=ApplicationOut)
def update_assist_session(session_id: str, body: AssistSessionUpdateBody) -> ApplicationOut:
    """The desktop runner's progress report for one assist session, looked up by
    assist_session_id (404 session_not_found if no application currently holds it). Updates only
    the fields provided: assist_state (validated), filled_fields (appended to assist_log, never
    replacing it) and approved_answers (upserted into AnswerBank via _insert_answer)."""
    if body.assist_state is not None and body.assist_state not in _ASSIST_STATES:
        raise _coded(422, "unknown_assist_state", f"assist_state must be one of {sorted(_ASSIST_STATES)}")
    with db.get_session() as session:
        row = session.exec(
            select(JobApplication).where(JobApplication.assist_session_id == session_id)
        ).first()
        if row is None:
            raise _coded(404, "session_not_found", "No application has this assist session id")

        if body.assist_state is not None:
            row.assist_state = body.assist_state

        if body.step is not None or body.filled_fields is not None:
            log = _json_list(row.assist_log)
            if body.filled_fields:
                for field in body.filled_fields:
                    entry = field.model_dump()
                    if body.step is not None:
                        entry["step"] = body.step
                    log.append(entry)
            else:
                log.append({"step": body.step})
            row.assist_log = json.dumps(log, ensure_ascii=False)

        if body.approved_answers is not None:
            for approved in body.approved_answers:
                _insert_answer(session, approved.question, approved.answer, source_application_id=row.id)

        session.add(row)
        session.commit()
        session.refresh(row)
        return _application_out(row)


class ProposeBody(BaseModel):
    """POST body for /jobs/assist/sessions/{session_id}/propose: the field descriptors the desktop
    runner's in-page extractor just produced (pipeline.jobs.assist.extract.extract_fields())."""

    model_config = ConfigDict(extra="forbid")

    fields: list[dict[str, Any]] = Field(max_length=_MAX_PROPOSE_FIELDS)


class FieldProposalOut(BaseModel):
    """One proposed value, mirroring pipeline.jobs.assist.mapping.FieldProposal's shape (`value` is
    deliberately loose there: text, a bool, or a select option)."""

    field_id: str
    value: Any
    confidence: float
    source: str
    needs_user: bool


@router.post("/jobs/assist/sessions/{session_id}/propose", response_model=list[FieldProposalOut])
def propose_session_values(session_id: str, body: ProposeBody) -> list[FieldProposalOut]:
    """Proposes a value for every extracted field of one assist session's page, via
    mapping.propose_values() — the only place the Anthropic key is used in the apply-assist flow
    (see module docstring). 404 session_not_found when no application holds this session id."""
    with db.get_session() as session:
        row = session.exec(
            select(JobApplication).where(JobApplication.assist_session_id == session_id)
        ).first()
        if row is None:
            raise _coded(404, "session_not_found", "No application has this assist session id")
        profile = _get_or_create_profile(session)
        proposals = mapping.propose_values(body.fields, profile)
    return [FieldProposalOut(**proposal.model_dump()) for proposal in proposals]


@router.get("/jobs/applications/{id}/files/{kind}")
def get_application_file(id: int, kind: Literal["cover_letter", "resume"]):
    """Streams the application's cover letter or resume PDF (see module docstring for the
    two-stage 404 check mirroring runs.py's GET /runs/{run_id}/pdf)."""
    with db.get_session() as session:
        row = session.get(JobApplication, id)
        if row is None:
            raise _coded(404, "application_not_found", "Application not found")
        stored_path = getattr(row, _FILE_PATH_FIELDS[kind])
    if not stored_path:
        raise _coded(404, "file_not_set", f"No {kind} file recorded for this application")
    path = Path(stored_path)
    if not path.exists():
        raise _coded(404, "file_missing", f"{kind} file missing on disk")
    return FileResponse(path, media_type="application/pdf", filename=path.name)
