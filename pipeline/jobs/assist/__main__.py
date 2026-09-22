"""
Desktop entry point for the guided apply-assist session (T-030; docs/_research/
2026-09-21_jobs-collector.md "Apply Assist" > "Browser choice", "Where it runs", "Page reading and
filling").

`python -m pipeline.jobs.assist <application_id>` walks one application; `python -m
pipeline.jobs.assist --watch` polls the server's assist queue and walks whatever it finds. Either
way a visible Chrome window opens on the user's own machine, the checklist overlay (T-029) rides
along, and the user is present for the whole session.

This module is the integration point for the rest of the package: client.py (server I/O),
extract.py (read the page), fill.py (fill it + the final-submit guard), overlay.py (the panel), and
pipeline.jobs.apply's playbooks (the ordered steps). Two properties are non-negotiable here:

- **The runner never submits.** Every click this module makes goes through fill.advance_step() ->
  fill.guarded_click(), and a control is only ever offered to that guard after
  fill.is_final_submit() has already rejected it here; at or after the playbook's `review` step the
  runner stops clicking anything at all and the overlay tells the user "ready: review and click
  Submit". Nothing here can move JobApplication.status to "submitted" either — the runner's only
  write path is client.report_progress(), whose server route has no `status` field by design.
- **The Anthropic API key never reaches this machine.** Field values come from
  POST /api/jobs/assist/sessions/{id}/propose (T-030's server route), which runs
  pipeline.jobs.assist.mapping server-side. This module deliberately does not import `mapping`, and
  must not: importing it would pull anthropic and ANTHROPIC_API_KEY onto the desktop.

Design decisions:

- Browser: `chromium.launch_persistent_context(user_data_dir=PROFILE_DIR, channel="chrome",
  headless=False)` per the research doc — headed so the user watches and can take over, with a
  dedicated persistent profile so Workday tenant logins and cookies survive between sessions. The
  `channel="chrome"` attempt is wrapped in a try/except that retries without `channel` (the bundled
  Chromium) so a machine with no real Chrome install still runs. `SIGNALSLATE_ASSIST_HEADLESS=1`
  flips it headless, which is how the test suite (and any CI box with no display) drives it.
- `_launch_context()` and `PROFILE_DIR` are module-level on purpose: a test overrides them
  (monkeypatch PROFILE_DIR to a tmp dir, set SIGNALSLATE_ASSIST_HEADLESS=1) and gets the real
  launch path, while `run_session(..., context_factory=...)` injects a whole alternative context
  manager when a caller wants to keep the browser open after the session returns (what
  tests/test_assist_session.py does, so it can inspect the live page).
- Overlay callbacks only *queue* an action; the loop performs it. Playwright's sync API dispatches
  an `expose_function` callback re-entrantly while the main thread sits inside some other
  Playwright call, so doing the HTTP round-trip and the fill work inside the callback itself would
  run them from an arbitrary point in the middle of another operation. Queueing keeps every
  Playwright/HTTP call on the loop's own thread of control, and the loop pumps the queue between
  steps and continuously while paused, so a button press still takes effect promptly.
- The loop drives the session itself (fill the step, advance, repeat) rather than waiting for the
  user to press "Fill page" for every step: the research doc's flow is "at each step it reads the
  page, fills every field it can... and clicks Next/Continue", with the buttons there for
  intervention. `user_only` steps (Workday's sign-in, email verification, Voluntary Disclosures,
  Self Identify) are the exception — the loop stops there and waits for the user to press "Next
  step", per PlaybookStep.user_only's own contract.
- CAPTCHA: any frame whose URL contains "recaptcha"/"hcaptcha" pauses the session (research doc:
  "No stealth plugins, no CAPTCHA solving... the session pauses and the overlay asks the user to
  solve it"). Checked before and after each step's fill, since a challenge frame often appears only
  once the form is touched.
- File fields (resume, cover letter) carry a *server-side* path in a proposal, which does not exist
  on this machine, so `_localize_file_proposals()` swaps in a locally downloaded copy via
  client.download_file(). A download that 404s (no packet rendered yet, nothing recorded) turns
  that field into a needs_user item for the human to attach by hand — never a silent skip.
- `_sleep` is a module-level indirection over time.sleep (the same pattern pipeline.domains.intel
  and fill.py use) so tests never actually wait out the polling intervals.
"""
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Optional

from playwright.sync_api import sync_playwright

from pipeline.jobs.apply import PlaybookStep, get_playbook
from pipeline.jobs.assist.client import (
    REQUIRED_HEADER,
    REQUIRED_HEADER_VALUE,
    AssistApiError,
    AssistClient,
)
from pipeline.jobs.assist.extract import extract_fields
from pipeline.jobs.assist.fill import SubmitGuardError, advance_step, apply_proposals, is_final_submit
from pipeline.jobs.assist.overlay import build_state, install_overlay, render

_log = logging.getLogger(__name__)

_sleep = time.sleep

PROFILE_DIR = Path.home() / ".local" / "share" / "signalslate" / "apply-profile"

HEADLESS_ENV = "SIGNALSLATE_ASSIST_HEADLESS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Queue polling (watch()) and UI polling (the loop's pump), in seconds.
_QUEUE_POLL_SECONDS = 3.0
_UI_POLL_SECONDS = 0.2
# Milliseconds the pump spends inside Playwright per tick: that is what lets the browser dispatch
# a pending overlay-button callback into Python at all.
_UI_POLL_MS = 100

_CAPTCHA_URL_RE = re.compile(r"recaptcha|hcaptcha", re.IGNORECASE)
_NEXT_TEXT_RE = re.compile(r"^(next( step)?|continue|save\s+and\s+continue)$", re.IGNORECASE)
_RESUME_LABEL_RE = re.compile(r"r[ée]sum[ée]|\bcv\b", re.IGNORECASE)
_COVER_LETTER_LABEL_RE = re.compile(r"cover\s*letter", re.IGNORECASE)

# Clickable controls in one frame, as {text, type, selector} — the shape fill.is_final_submit() /
# fill.advance_step() take. Kept here rather than in extract.js because that file's contract is
# *fillable fields* (it deliberately skips submit/button inputs); this is the navigation control
# scan, which only this module needs.
_CONTROLS_JS = """
() => {
  function cssPath(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node.tagName !== "HTML") {
      if (node.id) { parts.unshift("#" + CSS.escape(node.id)); break; }
      let index = 1;
      let sib = node;
      while ((sib = sib.previousElementSibling)) { if (sib.tagName === node.tagName) index++; }
      parts.unshift(node.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
      node = node.parentElement;
    }
    return parts.join(" > ");
  }
  const selector = 'button, input[type="submit"], input[type="button"], [role="button"]';
  const out = [];
  Array.prototype.forEach.call(document.querySelectorAll(selector), function (el) {
    if (el.closest("#__assist-overlay-host")) return;
    if (!el.getClientRects().length) return;
    const text = (el.innerText || el.value || el.getAttribute("aria-label") || "")
      .replace(/\\s+/g, " ")
      .trim();
    out.push({
      text: text,
      type: (el.getAttribute("type") || "").toLowerCase(),
      selector: cssPath(el)
    });
  });
  return out;
}
"""


class AssistSessionError(RuntimeError):
    """Raised when a session cannot be started at all (no session id from claim(), no posting, no
    apply_url) — a session that cannot even open the right page is not something to degrade
    through, the user is watching."""


# --- Browser launch --------------------------------------------------------------------------------


def headless() -> bool:
    """
    Whether to launch the browser headless.

    Returns:
        True when SIGNALSLATE_ASSIST_HEADLESS is set to a truthy value ("1"/"true"/"yes"/"on",
        case-insensitive) — how CI and the test suite run without a display. False otherwise: the
        session is headed by design, because the user watches it (see module docstring).
    """
    return os.environ.get(HEADLESS_ENV, "").strip().lower() in _TRUTHY


def _launch_context(playwright: Any, *, user_data_dir: Path, headless: bool) -> Any:
    """
    Launches the persistent browser context the session runs in.

    Args:
        playwright: The object `sync_playwright()` yielded.
        user_data_dir: Persistent profile directory (created by Playwright if absent), so tenant
            logins and cookies survive between sessions.
        headless: Passed straight to Playwright.

    Returns:
        The BrowserContext, launched with `channel="chrome"` (the user's real Chrome) when that
        works, otherwise retried with Playwright's bundled Chromium — a machine with no Chrome
        install still runs the assist (research doc: 'channel="chrome" if available').

    Raises:
        playwright.sync_api.Error: the bundled-Chromium retry also failed (no browser at all).
    """
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir), channel="chrome", headless=headless
        )
    except Exception as exc:
        _log.info("_launch_context: chrome channel unavailable (%s), using bundled chromium", type(exc).__name__)
        return playwright.chromium.launch_persistent_context(user_data_dir=str(user_data_dir), headless=headless)


@contextmanager
def _default_context() -> Iterator[Any]:
    """
    The default browser context for a session: a persistent context under PROFILE_DIR, closed (with
    its Playwright driver) when the session ends.

    Returns:
        A context manager yielding the BrowserContext.
    """
    with sync_playwright() as playwright:
        context = _launch_context(playwright, user_data_dir=PROFILE_DIR, headless=headless())
        try:
            yield context
        finally:
            context.close()


# --- Server calls the AssistClient doesn't cover ------------------------------------------------------


def propose(client: AssistClient, session_id: str, fields: list[dict]) -> list[dict]:
    """
    Asks the server to propose a value for every extracted field (POST
    /api/jobs/assist/sessions/{session_id}/propose).

    Lives here rather than on AssistClient because that module is out of this task's scope; it uses
    the same session/auth-header/error conventions AssistClient._request() does, so behaviour is
    identical to the client's own methods.

    Args:
        client: The AssistClient whose session/base_url to reuse.
        session_id: The assist session id claim() returned.
        fields: Field descriptors from extract_fields().

    Returns:
        A list of `{field_id, value, confidence, source, needs_user}` dicts, one per field the
        server could place (see mapping.propose_values()).

    Raises:
        AssistApiError: the server returned a non-2xx response (404 when no application holds this
            session id).
    """
    path = f"/api/jobs/assist/sessions/{session_id}/propose"
    response = client.session.request(
        "POST",
        f"{client.base_url}{path}",
        headers={REQUIRED_HEADER: REQUIRED_HEADER_VALUE},
        json={"fields": fields},
    )
    if response.status_code >= 400:
        raise AssistApiError.from_response("POST", path, response)
    return response.json()


# --- Page helpers -------------------------------------------------------------------------------------


def _frame_controls(frame: Any) -> list[dict]:
    """Every visible clickable control in one frame, as {text, type, selector} (see _CONTROLS_JS).
    A frame that has navigated/detached mid-scan contributes nothing rather than failing the scan."""
    try:
        return frame.evaluate(_CONTROLS_JS)
    except Exception as exc:
        _log.debug("_frame_controls: %s", type(exc).__name__)
        return []


def _find_next_control(page: Any) -> Optional[tuple[Any, dict]]:
    """
    The step's Next/Continue control, searched across the page's frames.

    Args:
        page: The Playwright Page.

    Returns:
        `(frame, locator_info)` for the first control whose text reads Next/Continue/Save and
        continue AND which fill.is_final_submit() does not reject, or None when the page has no
        such control (the user advances by hand). Submit-shaped controls are never returned, so the
        caller cannot even offer one to fill.advance_step().
    """
    for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
        for control in _frame_controls(frame):
            if not _NEXT_TEXT_RE.match((control.get("text") or "").strip()):
                continue
            if is_final_submit(control):
                continue
            return frame, control
    return None


def _captcha_frame_url(page: Any) -> Optional[str]:
    """The URL of the first reCAPTCHA/hCaptcha frame on the page, or None when there is none."""
    for frame in page.frames:
        url = frame.url or ""
        if _CAPTCHA_URL_RE.search(url):
            return url
    return None


# --- The session --------------------------------------------------------------------------------------


@dataclass
class _Session:
    """One guided application session's live state. Mutated by the loop, and (queue only) by the
    overlay's bridge callbacks."""

    client: AssistClient
    application_id: int
    session_id: str
    steps: list[PlaybookStep]
    page: Any
    index: int = 0
    paused: bool = False
    actions: list[tuple] = field(default_factory=list)
    fields: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    panel_fields: list[dict] = field(default_factory=list)
    needs_user: list[dict] = field(default_factory=list)
    _downloads: dict[str, Optional[str]] = field(default_factory=dict)
    _download_dir: Optional[str] = None

    # -- state -------------------------------------------------------------------------------------

    def current_step(self) -> Optional[PlaybookStep]:
        """The step the loop is on, or None once the playbook is exhausted."""
        if 0 <= self.index < len(self.steps):
            return self.steps[self.index]
        return None

    def render_panel(self) -> None:
        """Re-renders the overlay from this session's current step/fields/needs_user."""
        step = self.current_step()
        state = build_state(self.steps, step.id if step is not None else None, self.panel_fields, self.needs_user)
        render(self.page, state)

    # -- server ------------------------------------------------------------------------------------

    def report(self, **kwargs: Any) -> None:
        """Reports progress for this session (see client.report_progress() for the keywords)."""
        self.client.report_progress(self.session_id, **kwargs)

    def _local_file(self, kind: str) -> Optional[str]:
        """A locally downloaded copy of this application's `kind` file ("resume"/"cover_letter"),
        or None when the server has none recorded. Downloaded once per session per kind."""
        if kind in self._downloads:
            return self._downloads[kind]
        if self._download_dir is None:
            self._download_dir = tempfile.mkdtemp(prefix="signalslate-assist-")
        dest = str(Path(self._download_dir) / f"{kind}.pdf")
        try:
            self.client.download_file(self.application_id, kind, dest)
            self._downloads[kind] = dest
        except AssistApiError as exc:
            _log.info("_local_file(%s): %s", kind, type(exc).__name__)
            self._downloads[kind] = None
        except OSError as exc:
            _log.warning("_local_file(%s): %s", kind, type(exc).__name__)
            self._downloads[kind] = None
        return self._downloads[kind]

    # -- work --------------------------------------------------------------------------------------

    def _localize_file_proposals(self, fields: list[dict], proposals: list[dict]) -> list[dict]:
        """Replaces every file field's server-side path with a locally downloaded copy (see module
        docstring); a file the server cannot supply becomes a needs_user item instead."""
        fields_by_id = {item["field_id"]: item for item in fields}
        out: list[dict] = []
        for proposal in proposals:
            target = fields_by_id.get(proposal.get("field_id"))
            if target is None or (target.get("type") or "").lower() != "file":
                out.append(proposal)
                continue
            value = proposal.get("value")
            if value and Path(str(value)).exists():
                out.append(proposal)
                continue
            haystack = f"{target.get('label') or ''} {target.get('field_id') or ''}"
            if _COVER_LETTER_LABEL_RE.search(haystack):
                kind = "cover_letter"
            elif _RESUME_LABEL_RE.search(haystack):
                kind = "resume"
            else:
                out.append(proposal)
                continue
            local = self._local_file(kind)
            if local is None:
                out.append({**proposal, "value": None, "needs_user": True})
            else:
                out.append({**proposal, "value": local, "needs_user": False})
        return out

    def fill_step(self, step: PlaybookStep) -> None:
        """
        One fill iteration for the current step: extract -> server proposals -> fill -> re-render
        the overlay -> report progress. This is exactly what the overlay's "Fill page" button
        triggers too (via the action queue).
        """
        fields = extract_fields(self.page)
        proposals = propose(self.client, self.session_id, fields)
        proposals = self._localize_file_proposals(fields, proposals)
        results = apply_proposals(self.page, fields, proposals)

        self.fields = fields
        self.proposals = proposals
        fields_by_id = {item["field_id"]: item for item in fields}
        ok_by_id = {result["field_id"]: result for result in results}

        panel_fields: list[dict] = []
        needs_user: list[dict] = []
        filled: list[dict] = []
        for proposal in proposals:
            field_id = proposal.get("field_id")
            target = fields_by_id.get(field_id, {})
            label = target.get("label") or field_id
            if proposal.get("needs_user"):
                needs_user.append({"field_id": field_id, "label": label, "reason": "needs your answer"})
                continue
            result = ok_by_id.get(field_id)
            if result is None:
                continue
            if not result["ok"]:
                needs_user.append({"field_id": field_id, "label": label, "reason": "could not fill automatically"})
                continue
            value = "" if proposal.get("value") is None else str(proposal.get("value"))
            confidence = float(proposal.get("confidence") or 0.0)
            source = str(proposal.get("source") or "generated")
            panel_fields.append(
                {
                    "field_id": field_id,
                    "label": label,
                    "value": value,
                    "source": source,
                    "confidence": confidence,
                    "selector": target.get("selector"),
                    "needs_user": False,
                }
            )
            filled.append({"field_id": field_id, "value": value, "source": source, "confidence": confidence})

        self.panel_fields = panel_fields
        self.needs_user = needs_user
        self.render_panel()
        self.report(step=step.id, filled_fields=filled)

    def advance(self, step: PlaybookStep) -> bool:
        """
        Clicks this step's Next/Continue control, if the page has one.

        Never runs at or after the `review` step (the caller returns before then) and never sees a
        submit-shaped control: _find_next_control() filters those out and fill.advance_step()'s own
        guard is the second line of defence.

        Returns:
            True when a control was found and clicked; False when there was none, or the click
            failed / was refused — in which case the user advances the page by hand.
        """
        found = _find_next_control(self.page)
        if found is None:
            return False
        frame, control = found
        try:
            advance_step(frame, control)
        except SubmitGuardError:
            _log.warning("advance(%s): refused a final-submit control", step.id)
            return False
        except Exception as exc:
            _log.warning("advance(%s): %s", step.id, type(exc).__name__)
            return False
        self.page.wait_for_timeout(_UI_POLL_MS)
        return True

    def pause(self, reason: str) -> None:
        """Pauses the session (server-side state + overlay message) until the user presses Fill
        page or Next step."""
        if not self.paused:
            self.paused = True
            self.report(assist_state="paused")
        self.needs_user = [{"field_id": "", "label": reason, "reason": "paused"}]
        self.render_panel()

    def resume(self) -> None:
        """Clears a pause, telling the server the runner is working again."""
        if self.paused:
            self.paused = False
            self.report(assist_state="running")

    def announce_review(self) -> None:
        """The end of the runner's job: the review step is the human's. Reports the step (assist
        state stays "running" — pipeline.db's assist_state vocabulary has no "ready_for_review"
        value and this task does not change that enum) and says so in the overlay."""
        self.panel_fields = []
        self.needs_user = [
            {
                "field_id": "",
                "label": "Ready: review the application and click Submit yourself",
                "reason": "final submit is always yours",
            }
        ]
        self.render_panel()
        self.report(step="review", assist_state="running")

    def await_user(self, step: PlaybookStep, reason: str) -> None:
        """Hands a step to the user (a user_only playbook step, or one the runner could not
        advance) and pumps until they press a button. The step's own index moving on is what ends
        this wait."""
        self.needs_user = [{"field_id": "", "label": reason, "reason": reason}]
        self.render_panel()
        self.report(step=step.id)
        start_index = self.index
        while self.index == start_index and not self.page.is_closed():
            self.pump()

    # -- overlay bridge ----------------------------------------------------------------------------

    def queue(self, *action: Any) -> None:
        """Records one overlay-button action for the loop to perform (see module docstring for why
        the callbacks never do the work themselves)."""
        self.actions.append(tuple(action))

    def pump(self) -> None:
        """Performs any queued overlay actions, then gives the browser a moment to dispatch the
        next one."""
        while self.actions:
            action = self.actions.pop(0)
            self._perform(action)
        if self.page.is_closed():
            return
        _sleep(_UI_POLL_SECONDS)
        self.page.wait_for_timeout(_UI_POLL_MS)

    def _perform(self, action: tuple) -> None:
        """Runs one queued action. A failed action never kills the session — the user is watching
        and can retry from the panel."""
        name = action[0]
        step = self.current_step()
        try:
            if name == "pause":
                self.pause("Paused — press Fill page or Next step to resume")
            elif name == "fill" and step is not None and step.kind != "review":
                self.resume()
                self.fill_step(step)
            elif name == "next":
                self.resume()
                if step is not None and step.kind != "review":
                    self.advance(step)
                self.index += 1
            elif name == "edit":
                self._edit_answer(action[1], action[2])
        except Exception as exc:
            _log.warning("_perform(%s): %s", name, type(exc).__name__)

    def _edit_answer(self, field_id: str, new_value: Any) -> None:
        """Writes one user-corrected answer back to the answer bank (the server's approved_answers
        path) and reflects it in the panel."""
        target = next((item for item in self.fields if item["field_id"] == field_id), None)
        question = (target.get("label") if target else "") or field_id
        value = "" if new_value is None else str(new_value)
        self.report(approved_answers=[{"question": question, "answer": value}])
        for entry in self.panel_fields:
            if entry["field_id"] == field_id:
                entry["value"] = value
                entry["source"] = "answer_bank"
                entry["confidence"] = 1.0
                break
        self.needs_user = [item for item in self.needs_user if item.get("field_id") != field_id]
        self.render_panel()


def _ats_kind(data: dict) -> str:
    """The posting's ats_kind from a client.fetch_application() payload, or "" when the posting is
    missing one (get_playbook() falls back to the generic playbook for anything unknown)."""
    posting = data.get("posting") or {}
    return str(posting.get("ats_kind") or "")


def _apply_url(data: dict) -> str:
    """The posting's apply_url from a client.fetch_application() payload.

    Raises:
        AssistSessionError: the payload carries no posting, or no apply_url on it — there is no
            page to open.
    """
    posting = data.get("posting") or {}
    url = posting.get("apply_url")
    if not url:
        raise AssistSessionError("application's posting has no apply_url to open")
    return str(url)


def run_session(
    application_id: int,
    *,
    client: Optional[AssistClient] = None,
    context_factory: Optional[Callable[[], ContextManager[Any]]] = None,
) -> None:
    """
    Runs one guided apply-assist session from claim to the review step.

    Claims the application, fetches its posting/packet/profile, opens the posting's apply_url in a
    persistent Chrome context, installs the checklist overlay, then walks the ATS playbook: fill
    each fillable step (extract -> server-side proposals -> fill -> report), click its
    Next/Continue control, pause for CAPTCHAs, and stop at the `review` step with the overlay
    telling the user to review and submit themselves. The runner never clicks Submit and never
    marks the application submitted (see module docstring).

    Args:
        application_id: The JobApplication id to work. Must be queued for assist (POST
            /jobs/applications/{id}/assist) — claim() is what this call does first.
        client: The AssistClient to talk to the server with. Defaults to a fresh AssistClient()
            reading SIGNALSLATE_API_URL.
        context_factory: Zero-argument callable returning a context manager that yields a
            Playwright BrowserContext. Defaults to `_default_context` (a persistent context under
            PROFILE_DIR); tests inject one that keeps the browser open past the session's end.

    Returns:
        None. Returns when the review step is reached, the playbook is exhausted, or the user
        closes the browser window.

    Raises:
        AssistSessionError: the claim returned no assist_session_id, or the posting has no
            apply_url.
        AssistApiError: any server call failed (claim on a not-queued application, a fetch, a
            progress report).
        playwright.sync_api.Error: the browser could not be launched or the page could not be
            opened.
    """
    client = client or AssistClient()
    application = client.claim(application_id)
    session_id = application.get("assist_session_id")
    if not session_id:
        raise AssistSessionError(f"claim of application {application_id} returned no assist_session_id")

    data = client.fetch_application(application_id)
    steps = get_playbook(_ats_kind(data))
    url = _apply_url(data)

    factory = context_factory or _default_context
    with factory() as context:
        page = context.new_page()
        page.goto(url)

        session = _Session(
            client=client,
            application_id=application_id,
            session_id=str(session_id),
            steps=steps,
            page=page,
        )
        install_overlay(
            page,
            steps,
            {"current_step_id": steps[0].id if steps else None, "fields": [], "needs_user": []},
            on_fill=lambda: session.queue("fill"),
            on_next=lambda: session.queue("next"),
            on_edit_answer=lambda field_id, new_value: session.queue("edit", field_id, new_value),
            on_pause=lambda: session.queue("pause"),
        )
        session.report(step=steps[0].id if steps else None, assist_state="running")
        _run_loop(session)


def _run_loop(session: _Session) -> None:
    """The guided loop itself: one iteration per playbook step (see run_session()'s docstring for
    the contract it implements)."""
    while not session.page.is_closed():
        step = session.current_step()
        if step is None:
            return

        if session.paused:
            session.pump()
            continue

        captcha_url = _captcha_frame_url(session.page)
        if captcha_url is not None:
            _log.info("_run_loop: captcha frame detected, pausing")
            session.pause("Solve the CAPTCHA, then press Next step")
            continue

        if step.kind == "review":
            session.announce_review()
            return

        if step.user_only:
            session.await_user(step, f"{step.title}: this step is yours — press Next step when done")
            continue

        session.fill_step(step)

        captcha_url = _captcha_frame_url(session.page)
        if captcha_url is not None:
            _log.info("_run_loop: captcha frame appeared after fill, pausing")
            session.pause("Solve the CAPTCHA, then press Next step")
            continue

        session.pump()
        if session.current_step() is not step:
            # The user pressed Next step while the fill was running; they already advanced.
            continue
        if not session.advance(step):
            session.await_user(step, f"{step.title}: no Next control found — advance the page, then press Next step")
            continue
        session.index += 1


def watch(*, client: Optional[AssistClient] = None) -> None:
    """
    Polls the server's assist queue forever, running a guided session for whatever it finds.

    One application at a time, user present (research doc's pacing guardrail): each queued
    application is walked to completion before the next poll. A session that raises is logged (its
    exception class only, this codebase's convention) and the loop continues — one bad application
    never kills the watcher.

    Args:
        client: The AssistClient to poll with. Defaults to a fresh AssistClient().

    Returns:
        None. Only returns if the caller interrupts it (KeyboardInterrupt propagates).
    """
    client = client or AssistClient()
    while True:
        try:
            queued = client.next_queued()
        except Exception as exc:
            _log.warning("watch: queue poll failed: %s", type(exc).__name__)
            queued = None
        if queued is not None:
            try:
                run_session(int(queued["id"]), client=client)
            except Exception as exc:
                _log.warning("watch: session for application %s failed: %s", queued.get("id"), type(exc).__name__)
        _sleep(_QUEUE_POLL_SECONDS)


def main(argv: list[str]) -> int:
    """
    CLI entry point: `python -m pipeline.jobs.assist <application_id>` or `--watch`.

    Args:
        argv: Arguments after the module name (i.e. `sys.argv[1:]`).

    Returns:
        Process exit code: 0 on success, 2 on a usage error.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) == 1 and argv[0] == "--watch":
        watch()
        return 0
    if len(argv) == 1 and argv[0].isdigit():
        run_session(int(argv[0]))
        return 0
    print("usage: python -m pipeline.jobs.assist <application_id> | --watch", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
