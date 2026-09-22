"""
End-to-end test for the whole jobs-collector subsystem (T-001..T-036), driven in pipeline order:

    seed -> resolve -> poll -> score -> digest item -> application + packet -> guided assist

Nothing under pipeline/ or api/ is mocked. The only doubles are true externals, exactly as
docs/_research/2026-09-21_jobs-collector.md's test plan prescribes ("Mock only true externals: ATS
HTTP (requests stubbed at import site with synthetic payloads), Anthropic (fake client). Real DB
(temp engine), real FastAPI app, headless Chromium against the multistep fixture."):

- ATS HTTP: `requests.get` is stubbed at pipeline.jobs.ats's own import site (the module object
  pipeline.jobs.resolve imports too, so the ladder's homepage fetch is covered by the same stub),
  following tests/test_ats_greenhouse_lever.py's route()/FakeResponse convention. The synthetic
  payload is a real Greenhouse board-API body for slug "acme" with one posting, so the resolver's
  slug probe and the poller's list_postings() both run for real against it.
- Anthropic: one FakeAnthropic serves all three model call sites in the flow (scoring's JobFitBatch,
  the packet's _ApplyPacket, the /propose route's mapping fallback), dispatching on the
  `output_format` each caller passes. Every deterministic path around those calls — the prefilter,
  the alias round-trip, the PDF render, the mapping ladder — is the real implementation.
- Everything else is real: a temp SQLite engine, a real FastAPI app with the jobs + job_apply
  routers and install_security, and a real headless Chromium (SIGNALSLATE_ASSIST_HEADLESS=1) walking
  tests/fixtures/jobs_forms/multistep_form.html through pipeline.jobs.assist.__main__.run_session().

The posting's apply_url is the local multistep fixture's file:// URL — that is what makes one
synthetic Greenhouse posting flow all the way into a real browser session without a network.

Two assertions are the point of the whole file:

1. The final-submit guard holds: the fixture's `window.__submitted` (settable only by its real
   "Submit Application" click handler) is still False, and JobApplication.status is still "ready"
   with submitted_at unset — only the T-023 PATCH route this test never calls could change that.
2. No profile PII leaks: the synthetic full_name/email/phone never appear in any log record
   captured at DEBUG across the entire flow, nor in the digest Items' payloads.
"""
import json
import logging
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import job_apply as job_apply_router  # noqa: E402
from api.routers import jobs as jobs_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors.jobs import collect_jobs  # noqa: E402
from pipeline.db import JobApplication, JobBoard, JobPosting, JobsProfile  # noqa: E402
from pipeline.jobs import ats, apply, packet, scoring  # noqa: E402
from pipeline.jobs.assist import __main__ as runner  # noqa: E402
from pipeline.jobs.assist.client import AssistClient  # noqa: E402
from pipeline.jobs.inventory import poll_all, resolve_pending  # noqa: E402
from pipeline.jobs.packet import ScreeningDraft, prepare_packet  # noqa: E402
from pipeline.jobs.seeds import add_company  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "jobs_forms" / "multistep_form.html"
FIXTURE_URL = f"file://{FIXTURE_PATH}"

MODEL = "test-model"

COMPANY_NAME = "Acme Inc"
COMPANY_DOMAIN = "acme.com"
BOARD_SLUG = "acme"
GREENHOUSE_JOBS_URL = f"https://boards-api.greenhouse.io/v1/boards/{BOARD_SLUG}/jobs"

POSTING_EXTERNAL_ID = "4200"
POSTING_TITLE = "Senior Software Engineer"
POSTING_LOCATION = "Remote"

# One synthetic Greenhouse board-API body, shaped exactly like tests/test_ats_greenhouse_lever.py's
# fixture. absolute_url points at the local multistep form so the same posting the collector scored
# is the page the assist session actually opens.
GREENHOUSE_BOARD = {
    "jobs": [
        {
            "id": int(POSTING_EXTERNAL_ID),
            "title": POSTING_TITLE,
            "absolute_url": FIXTURE_URL,
            "location": {"name": POSTING_LOCATION},
            "content": "<p>Build and operate backend services.</p>",
            "updated_at": "2026-09-20T00:00:00Z",
        }
    ]
}

# Synthetic PII. Deliberately unmistakable strings: every one is asserted absent from the captured
# logs and from the digest payloads at the end of the flow.
PROFILE_NAME = "Zorbulon Quibblesworth"
PROFILE_EMAIL = "zorbulon.quibblesworth@example.invalid"
PROFILE_PHONE = "+1-555-0143"
PII_VALUES = (PROFILE_NAME, PROFILE_EMAIL, PROFILE_PHONE)

FIT_SCORE = 87
FIT_REASON = "Backend role matches the stated target role and remote preference."
COVER_LETTER_TEXT = "Dear Acme hiring team, I would be glad to build backend services with you."
SCREENING_QUESTION = "Why do you want to work here?"
SCREENING_ANSWER = "The backend platform work lines up with what I want to build next."


# --- True externals -------------------------------------------------------------------------------


class FakeResponse:
    """requests.Response stand-in, per tests/test_ats_greenhouse_lever.py's FakeResponse."""

    def __init__(self, json_data: Any = None, status_code: int = 200, text: str = "") -> None:
        self._json = json_data
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = text

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeResponseMeta(SimpleNamespace):
    """Container for the stub's call log, so the test can assert which endpoints were hit."""


class FakeAnthropic:
    """
    Scripted stand-in for anthropic.Anthropic (a true external), shared by all three model call
    sites in this flow and dispatching on the caller's own `output_format` model:

    - scoring.JobFitBatch -> one JobFit per posting in the request, keyed by the request's own
      aliases (so the real alias round-trip in score_new() is what maps scores back onto rows).
    - packet._ApplyPacket -> a cover letter plus one screening draft.
    - mapping._GeneratedBatch -> an empty batch, so a field that reached the model step would come
      back needs_user rather than silently filled (this fixture's fields never should).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        name = getattr(kwargs.get("output_format"), "__name__", "")
        if name == "JobFitBatch":
            payload = json.loads(kwargs["messages"][0]["content"])
            items = [
                scoring.JobFit(alias=posting["alias"], score=FIT_SCORE, reason=FIT_REASON)
                for posting in payload["postings"]
            ]
            return SimpleNamespace(stop_reason="end_turn", parsed_output=SimpleNamespace(items=items))
        if name == "_ApplyPacket":
            return SimpleNamespace(
                stop_reason="end_turn",
                parsed_output=SimpleNamespace(
                    cover_letter_text=COVER_LETTER_TEXT,
                    screening_drafts=[
                        ScreeningDraft(question=SCREENING_QUESTION, answer=SCREENING_ANSWER)
                    ],
                ),
            )
        return SimpleNamespace(stop_reason="end_turn", parsed_output=SimpleNamespace(fields=[]))

    def output_formats(self) -> list[str]:
        """The `output_format` model name of every parse() call made so far, in order."""
        return [getattr(call.get("output_format"), "__name__", "") for call in self.calls]


# --- fixtures -------------------------------------------------------------------------------------


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """A temp SQLite engine swapped in for pipeline.db.engine, per tests/test_api_domain_buy.py."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def ats_http(monkeypatch) -> FakeResponseMeta:
    """
    The one network double: requests.get, stubbed at pipeline.jobs.ats's import site.

    Serves the synthetic Greenhouse board for slug "acme" (the resolver's probe and the poller's
    list_postings() hit the same endpoint) and 404s everything else, so a wrong slug guess or a
    careers-page fingerprint fetch degrades exactly as it would against a real 404 — never a real
    request.
    """
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if url == GREENHOUSE_JOBS_URL:
            return FakeResponse(json_data=GREENHOUSE_BOARD)
        return FakeResponse(status_code=404, text="")

    monkeypatch.setattr(ats.requests, "get", fake_get)
    monkeypatch.setattr(ats, "_sleep", lambda _seconds: None)
    return FakeResponseMeta(calls=calls)


@pytest.fixture
def fake_anthropic(monkeypatch) -> FakeAnthropic:
    """One fake client for every model call site. Wired into the /propose route's own import site
    (the runner never gets a key — see pipeline.jobs.assist.__main__'s docstring); the scoring and
    packet calls take it as an explicit `client=` argument instead."""
    fake = FakeAnthropic()
    monkeypatch.setattr(job_apply_router.mapping, "make_client", lambda: fake)
    monkeypatch.setattr(job_apply_router.mapping, "llm_settings", lambda: {"map_model": MODEL})
    return fake


@pytest.fixture
def test_client(temp_db, tmp_path, monkeypatch) -> TestClient:
    """The real app: jobs + job_apply routers behind install_security, on the temp DB."""
    monkeypatch.setattr(job_apply_router, "DB_PATH", tmp_path / "digest.db")
    monkeypatch.setattr(packet, "DB_PATH", tmp_path / "digest.db")
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(job_apply_router.router, prefix="/api")
    app.include_router(jobs_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


@pytest.fixture
def assist_client(test_client) -> AssistClient:
    return AssistClient(base_url="http://testserver", session=test_client)


@pytest.fixture
def open_context(monkeypatch, tmp_path):
    """A real headless Chromium context that stays open after run_session() returns, so the test can
    inspect the live page (filled values, step reached, window.__submitted) — the same shape
    tests/test_assist_session.py uses."""
    monkeypatch.setenv(runner.HEADLESS_ENV, "1")
    monkeypatch.setattr(runner, "PROFILE_DIR", tmp_path / "apply-profile")
    monkeypatch.setattr(runner, "_sleep", lambda _seconds: None)

    with sync_playwright() as playwright:
        context = runner._launch_context(
            playwright, user_data_dir=runner.PROFILE_DIR, headless=runner.headless()
        )

        @contextmanager
        def factory():
            yield context

        try:
            yield SimpleNamespace(context=context, factory=factory)
        finally:
            context.close()


@pytest.fixture
def resume_file(tmp_path) -> Path:
    """A real file on disk for the profile's resume path, so the fixture's file input is attached
    through the real set_input_files() path rather than left for the user."""
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\n% synthetic resume for the e2e test\n")
    return path


# --- helpers --------------------------------------------------------------------------------------


def _seed_profile(resume_path: Path) -> None:
    """The singleton JobsProfile (id=1) every later step reads: targeting for the prefilter, contact
    info for the packet prompt and the assist mapping."""
    with db.get_session() as session:
        session.add(
            JobsProfile(
                id=1,
                full_name=PROFILE_NAME,
                email=PROFILE_EMAIL,
                phone=PROFILE_PHONE,
                resume_paths=json.dumps([str(resume_path)]),
                eeo_answers="{}",
                target_roles=json.dumps(["Software Engineer"]),
                target_locations=json.dumps([POSTING_LOCATION]),
                cover_letter_tone="professional",
            )
        )
        session.commit()


def _reload_application(application_id: int) -> JobApplication:
    with db.get_session() as session:
        row = session.get(JobApplication, application_id)
        assert row is not None
        session.expunge(row)
        return row


def _assert_no_pii(haystack: str, where: str) -> None:
    for value in PII_VALUES:
        assert value not in haystack, f"profile PII leaked into {where}: {value!r}"


# --- the pipeline ------------------------------------------------------------------------------------


def test_jobs_pipeline_seed_to_assist_never_submits(
    test_client, assist_client, ats_http, fake_anthropic, open_context, resume_file, caplog
):
    """One company through the whole jobs subsystem: seeded, resolved to a Greenhouse board, polled
    into a posting, scored, surfaced as a digest item, turned into an application with a rendered
    packet, and walked by a real browser session that fills the form and stops dead at Submit."""
    caplog.set_level(logging.DEBUG)
    window_start = utcnow() - timedelta(hours=1)
    _seed_profile(resume_file)

    # 1. Seed -------------------------------------------------------------------------------------
    with db.get_session() as session:
        company = add_company(session, COMPANY_NAME, domain=COMPANY_DOMAIN, source="manual")
        session.commit()
        session.refresh(company)
        company_id = company.id
    assert company_id is not None

    # 2. Resolve ----------------------------------------------------------------------------------
    with db.get_session() as session:
        resolve_summary = resolve_pending(session)
    assert resolve_summary.resolved == 1
    assert resolve_summary.llm_used == 0  # the free slug probe hit; the paid research step never ran

    with db.get_session() as session:
        board = session.exec(select(JobBoard).where(JobBoard.company_id == company_id)).one()
        assert board.ats_kind == "greenhouse"
        assert board.board_id == BOARD_SLUG
        assert board.resolved_by == "slug_probe"
        board_id = board.id
    assert GREENHOUSE_JOBS_URL in ats_http.calls

    # 3. Poll -------------------------------------------------------------------------------------
    with db.get_session() as session:
        poll_summary = poll_all(session)
    assert (poll_summary.boards_polled, poll_summary.boards_failed) == (1, 0)
    assert poll_summary.postings_upserted == 1

    with db.get_session() as session:
        posting = session.exec(select(JobPosting).where(JobPosting.board_id == board_id)).one()
        assert posting.external_id == POSTING_EXTERNAL_ID
        assert posting.title == POSTING_TITLE
        assert posting.location == POSTING_LOCATION
        assert posting.apply_url == FIXTURE_URL
        assert posting.fit_score is None  # not scored yet
        posting_id = posting.id
    assert posting_id is not None

    # 4. Score ------------------------------------------------------------------------------------
    with db.get_session() as session:
        profile = session.get(JobsProfile, 1)
        assert profile is not None
        score_summary = scoring.score_new(session, profile, client=fake_anthropic, model=MODEL)
    assert (score_summary.scored, score_summary.filtered, score_summary.failed) == (1, 0, 0)
    assert "JobFitBatch" in fake_anthropic.output_formats()

    with db.get_session() as session:
        posting = session.get(JobPosting, posting_id)
        assert posting is not None
        assert posting.fit_score == FIT_SCORE
        assert posting.fit_reason == FIT_REASON

    # The same scored posting is what the API serves the UI.
    listed = test_client.get("/api/jobs/postings", params={"min_score": 60, "status": "open"})
    assert listed.status_code == 200
    assert [row["title"] for row in listed.json()] == [POSTING_TITLE]

    # 5. Digest item ------------------------------------------------------------------------------
    result = collect_jobs(window_start, utcnow() + timedelta(hours=1))
    assert result.status == "ok"
    assert [item.item_type for item in result.items] == ["job_new"]
    digest_item = result.items[0]
    assert digest_item.external_id == f"job_new:{posting_id}"
    assert digest_item.payload["posting_id"] == posting_id
    assert digest_item.payload["title"] == POSTING_TITLE
    assert digest_item.payload["company"] == COMPANY_NAME
    assert digest_item.payload["fit_score"] == FIT_SCORE

    # 6. Application + packet ----------------------------------------------------------------------
    created = test_client.post("/api/jobs/applications", json={"posting_id": posting_id})
    assert created.status_code == 201
    application_id = created.json()["id"]
    assert created.json()["status"] == "saved"

    with db.get_session() as session:
        application = session.get(JobApplication, application_id)
        assert application is not None
        apply.transition(application, "preparing")
        apply.transition(application, "ready")  # saved -> preparing -> ready, per _ALLOWED_TRANSITIONS
        session.add(application)
        session.commit()
        ok, reason = prepare_packet(session, application_id, client=fake_anthropic, model=MODEL)
    assert (ok, reason) == (True, "")
    assert "_ApplyPacket" in fake_anthropic.output_formats()

    prepared = _reload_application(application_id)
    assert prepared.status == "ready"
    packet_data = json.loads(prepared.packet)
    assert packet_data["cover_letter_text"] == COVER_LETTER_TEXT
    assert packet_data["screening_drafts"] == [
        {"question": SCREENING_QUESTION, "answer": SCREENING_ANSWER, "source": "generated"}
    ]
    cover_letter = Path(prepared.cover_letter_path)
    assert cover_letter.exists()
    with cover_letter.open("rb") as handle:
        assert handle.read(5) == b"%PDF-"

    # 7. Assist fills the form and stops at submit ---------------------------------------------------
    queued = test_client.post(f"/api/jobs/applications/{application_id}/assist")
    assert queued.status_code == 200
    assert queued.json()["assist_state"] == "queued"

    runner.run_session(application_id, client=assist_client, context_factory=open_context.factory)

    page = open_context.context.pages[-1]
    # Step 1's fields were filled through the server's own /propose route...
    assert page.input_value("#full_name") == PROFILE_NAME
    assert page.input_value("#email") == PROFILE_EMAIL
    assert page.eval_on_selector("#resume", "el => el.files.length") == 1
    # ...the runner clicked the step's Next control and reached the review page...
    assert page.locator("#step-2").is_visible()
    assert page.evaluate("window.__assistOverlayInstalled") is True
    # ...and the one assertion this whole file exists for: Submit was never clicked.
    assert page.evaluate("window.__submitted") is False

    assisted = _reload_application(application_id)
    assert assisted.assist_state == "running"
    assert assisted.status == "ready"  # only the T-023 PATCH route could make this "submitted"
    assert assisted.submitted_at is None

    log_entries = json.loads(assisted.assist_log or "[]")
    assert "form" in [entry.get("step") for entry in log_entries]
    assert "review" in [entry.get("step") for entry in log_entries]
    filled = {entry["field_id"]: entry for entry in log_entries if "value" in entry}
    assert filled["main:full_name"]["value"] == PROFILE_NAME
    assert filled["main:full_name"]["source"] == "profile"
    assert filled["main:email"]["value"] == PROFILE_EMAIL
    assert filled["main:resume"]["value"] == str(resume_file)
    # The deterministic profile/resume rules covered every field, so the mapping model step never ran.
    assert "_GeneratedBatch" not in fake_anthropic.output_formats()

    # No application_update event either: nothing in this flow stamped submitted_at.
    after = collect_jobs(window_start, utcnow() + timedelta(hours=1))
    assert [item.item_type for item in after.items] == ["job_new"]

    # --- No profile PII in logs or digest payloads -------------------------------------------------
    # Guard against a vacuous check: the flow above does log (fetches, fills, session progress), so
    # an empty capture would mean the assertions below are searching nothing.
    assert caplog.records, "nothing was captured at DEBUG; the PII check below would be vacuous"
    for record in caplog.records:
        _assert_no_pii(record.getMessage(), f"log record {record.name}")
    _assert_no_pii(caplog.text, "captured logs")
    _assert_no_pii(json.dumps([item.payload for item in after.items], default=str), "digest payloads")
