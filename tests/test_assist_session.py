"""
Integration tests for the guided apply-assist session (T-030): pipeline.jobs.assist.__main__'s
run_session()/watch() and the server route they lean on, POST
/api/jobs/assist/sessions/{session_id}/propose.

Nothing under pipeline/ or api/ is mocked. A real FastAPI app (job_apply + jobs routers,
install_security, a temp sqlite DB) is driven through Starlette's TestClient, injected as
AssistClient's `session` the same way tests/test_assist_client.py does, and a real Chromium is
launched through the runner's own `_launch_context()` (headless via SIGNALSLATE_ASSIST_HEADLESS=1,
so a CI box with no display can run it) against tests/fixtures/jobs_forms/multistep_form.html over
file://. The one fake is the Anthropic client behind the /propose route's mapping.propose_values()
call — a true external — so the deterministic profile rules and the answer-bank lookup run for
real and only the Claude fallback step is scripted. For this fixture's fields (Full Name, Email,
Resume) the deterministic ladder covers everything, which the fake's own empty call log asserts.

The load-bearing assertion is the last one: the fixture's window.__submitted flag, settable only by
its real "Submit Application" button's click handler, is still false when the session ends.
"""
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import job_apply as job_apply_router  # noqa: E402
from api.routers import jobs as jobs_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import db  # noqa: E402
from pipeline.db import JobApplication, JobBoard, JobCompany, JobPosting, JobsProfile  # noqa: E402
from pipeline.jobs.assist import __main__ as runner  # noqa: E402
from pipeline.jobs.assist.client import AssistClient  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "jobs_forms" / "multistep_form.html"
FIXTURE_URL = f"file://{FIXTURE_PATH}"

PROFILE_NAME = "Ada Lovelace"
PROFILE_EMAIL = "ada@example.com"


class _StopWatch(Exception):
    """Sentinel raised from the patched _sleep to break watch()'s otherwise endless poll loop.
    watch() only catches per-session failures, so this propagates — reaching the `pytest.raises`
    below is itself the proof the loop got all the way back around to its next poll."""


def _stop_at_queue_poll(seconds: float) -> None:
    """_sleep replacement for the watch() tests: instant for the session's own UI pump, a _StopWatch
    for the queue poll that ends one watch iteration."""
    if seconds == runner._QUEUE_POLL_SECONDS:
        raise _StopWatch()


class FakeAnthropic:
    """Scripted stand-in for anthropic.Anthropic (a true external), in tests/test_assist_mapping.py's
    style: records every parse() call and returns an empty batch, so a field that reached the model
    step would come back needs_user rather than silently filled."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=SimpleNamespace(fields=[]))


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def fake_anthropic(monkeypatch) -> FakeAnthropic:
    """Wired in at the /propose route's own import site, so propose_values() never reaches the
    network and the test needs no ANTHROPIC_API_KEY."""
    fake = FakeAnthropic()
    monkeypatch.setattr(job_apply_router.mapping, "make_client", lambda: fake)
    monkeypatch.setattr(job_apply_router.mapping, "llm_settings", lambda: {"map_model": "test-model"})
    return fake


@pytest.fixture
def test_client(temp_db, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(job_apply_router, "DB_PATH", tmp_path / "digest.db")
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(job_apply_router.router, prefix="/api")
    app.include_router(jobs_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


@pytest.fixture
def client(test_client) -> AssistClient:
    return AssistClient(base_url="http://testserver", session=test_client)


@pytest.fixture
def headless_runner(monkeypatch, tmp_path):
    """The runner as a test drives it: headless (no display in CI), a throwaway persistent profile
    directory, and no real waiting in its poll loops."""
    monkeypatch.setenv(runner.HEADLESS_ENV, "1")
    monkeypatch.setattr(runner, "PROFILE_DIR", tmp_path / "apply-profile")
    monkeypatch.setattr(runner, "_sleep", lambda _seconds: None)


@pytest.fixture
def open_context(headless_runner):
    """A context_factory that hands run_session a real persistent Chromium context and, unlike the
    runner's own _default_context, leaves it open when the session returns so the test can inspect
    the live page (values filled, step reached, window.__submitted)."""
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


def _seed_profile() -> None:
    with db.get_session() as session:
        session.add(
            JobsProfile(
                id=1,
                full_name=PROFILE_NAME,
                email=PROFILE_EMAIL,
                phone="555-0100",
                resume_paths="[]",
                eeo_answers="{}",
            )
        )
        session.commit()


def _seed_application(apply_url: str = FIXTURE_URL, **overrides) -> JobApplication:
    """A queued, ready application on a greenhouse posting (so get_playbook() returns the generic
    fill-then-review playbook) pointing at the given apply_url."""
    with db.get_session() as session:
        company = JobCompany(name="Acme Corp", domain="acme.com", source="manual", status="active")
        session.add(company)
        session.commit()
        session.refresh(company)
        board = JobBoard(
            company_id=company.id,
            ats_kind="greenhouse",
            board_id="acme",
            resolved_by="pattern",
            confidence=1.0,
        )
        session.add(board)
        session.commit()
        session.refresh(board)
        posting = JobPosting(
            board_id=board.id,
            external_id="1",
            title="Senior Engineer",
            apply_url=apply_url,
            content_hash="hash1",
        )
        session.add(posting)
        session.commit()
        session.refresh(posting)
        fields = {"posting_id": posting.id, "status": "ready", "assist_state": "queued"}
        fields.update(overrides)
        application = JobApplication(**fields)
        session.add(application)
        session.commit()
        session.refresh(application)
        session.expunge(application)
        return application


def _reload(application_id: int) -> JobApplication:
    with db.get_session() as session:
        row = session.get(JobApplication, application_id)
        assert row is not None
        session.expunge(row)
        return row


def _log_steps(application: JobApplication) -> list[str]:
    return [entry.get("step") for entry in json.loads(application.assist_log or "[]")]


# --- POST /jobs/assist/sessions/{session_id}/propose ---------------------------------------------


def test_propose_returns_deterministic_profile_values(test_client, fake_anthropic):
    _seed_profile()
    application = _seed_application()
    session_id = test_client.post(f"/api/jobs/assist/queue/{application.id}/claim").json()["assist_session_id"]

    response = test_client.post(
        f"/api/jobs/assist/sessions/{session_id}/propose",
        json={
            "fields": [
                {"field_id": "main:full_name", "label": "Full Name", "type": "text", "options": []},
                {"field_id": "main:email", "label": "Email", "type": "email", "options": []},
            ]
        },
    )

    assert response.status_code == 200
    by_id = {item["field_id"]: item for item in response.json()}
    assert by_id["main:full_name"]["value"] == PROFILE_NAME
    assert by_id["main:full_name"]["source"] == "profile"
    assert by_id["main:email"]["value"] == PROFILE_EMAIL
    assert fake_anthropic.calls == []  # deterministic ladder covered both fields; no model call


def test_propose_unknown_session_is_404(test_client, fake_anthropic):
    response = test_client.post("/api/jobs/assist/sessions/nope/propose", json={"fields": []})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "session_not_found"


def test_propose_rejects_unknown_body_keys(test_client, fake_anthropic):
    response = test_client.post("/api/jobs/assist/sessions/nope/propose", json={"fields": [], "extra": 1})
    assert response.status_code == 422


# --- run_session -----------------------------------------------------------------------------------


def test_run_session_fills_advances_and_stops_at_review(client, open_context, fake_anthropic):
    _seed_profile()
    application = _seed_application()

    runner.run_session(application.id, client=client, context_factory=open_context.factory)

    page = open_context.context.pages[-1]
    # The fixture's step 1 fields were filled from the profile, through the server's /propose route.
    assert page.input_value("#full_name") == PROFILE_NAME
    assert page.input_value("#email") == PROFILE_EMAIL
    # ...and the runner clicked the step's own "Next" control, reaching the review page.
    assert page.locator("#step-2").is_visible()
    # The whole point: the review step's Submit button was never clicked.
    assert page.evaluate("window.__submitted") is False
    assert page.evaluate("window.__assistOverlayInstalled") is True

    reloaded = _reload(application.id)
    assert reloaded.assist_state == "running"
    assert reloaded.status == "ready"  # the runner never marks an application submitted
    assert reloaded.submitted_at is None

    log = json.loads(reloaded.assist_log)
    # One progress report per step, plus one entry per field actually filled.
    assert "form" in _log_steps(reloaded)
    assert "review" in _log_steps(reloaded)
    filled = {entry["field_id"]: entry for entry in log if "value" in entry}
    assert filled["main:full_name"]["value"] == PROFILE_NAME
    assert filled["main:full_name"]["source"] == "profile"
    assert filled["main:email"]["value"] == PROFILE_EMAIL
    # The resume upload had no file on the server to attach, so it was left for the user.
    assert "main:resume" not in filled


def test_run_session_overlay_shows_ready_for_review(client, open_context, fake_anthropic):
    _seed_profile()
    application = _seed_application()

    runner.run_session(application.id, client=client, context_factory=open_context.factory)

    page = open_context.context.pages[-1]
    panel = page.locator("#__assist-overlay-host").locator("div.assist-panel").inner_text()
    assert "Review" in panel
    assert "review the application and click Submit yourself" in panel


def test_run_session_requires_a_queued_application(client, open_context, fake_anthropic):
    from pipeline.jobs.assist.client import AssistApiError

    _seed_profile()
    application = _seed_application(assist_state="idle")

    with pytest.raises(AssistApiError) as excinfo:
        runner.run_session(application.id, client=client, context_factory=open_context.factory)
    assert excinfo.value.code == "not_queued"


# --- watch -------------------------------------------------------------------------------------------


def test_watch_runs_the_queued_application(client, headless_runner, monkeypatch, fake_anthropic):
    """watch() drives a whole session through the runner's own default browser launch (no injected
    context factory), then comes back around to its next poll — where the patched _sleep stops it."""
    _seed_profile()
    application = _seed_application()

    monkeypatch.setattr(runner, "_sleep", _stop_at_queue_poll)

    with pytest.raises(_StopWatch):
        runner.watch(client=client)

    reloaded = _reload(application.id)
    assert reloaded.assist_state == "running"
    assert "review" in _log_steps(reloaded)


def test_watch_survives_a_failing_session(client, headless_runner, monkeypatch, tmp_path, fake_anthropic):
    """A session that raises (here: an apply_url pointing at a file that does not exist) is logged
    and the watcher keeps polling — reaching the next _sleep is the proof."""
    _seed_profile()
    application = _seed_application(apply_url=f"file://{tmp_path / 'missing.html'}")

    monkeypatch.setattr(runner, "_sleep", _stop_at_queue_poll)

    with pytest.raises(_StopWatch):
        runner.watch(client=client)

    reloaded = _reload(application.id)
    assert reloaded.assist_state == "claimed"  # claimed, then the session failed before reporting


# --- headless switch ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False)],
)
def test_headless_env(monkeypatch, value, expected):
    monkeypatch.setenv(runner.HEADLESS_ENV, value)
    assert runner.headless() is expected


def test_headless_defaults_to_visible(monkeypatch):
    monkeypatch.delenv(runner.HEADLESS_ENV, raising=False)
    assert runner.headless() is False
