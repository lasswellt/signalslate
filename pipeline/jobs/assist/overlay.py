"""
Shadow-DOM checklist overlay for the desktop apply-assist runner (T-029, pipeline.jobs.assist).

Injects overlay.js (loaded from the sibling file, never inlined as a Python string literal, so the
two files stay independently readable/editable — the same convention extract.py uses for
extract.js) into the page, wires its four buttons to caller-supplied Python callbacks via
Playwright's `page.expose_function()`, and re-renders the panel's contents on demand via
`page.evaluate()`. This module has no opinion on what those callbacks do — it never calls anything
from pipeline.jobs.assist.fill's guarded_click/final-submit machinery itself; it only renders state
and forwards button clicks to whatever the caller wired up.

Design decisions:

- `page.add_init_script(overlay_js_source)` re-installs the overlay on every new document the page
  navigates to (Workday-style multi-page applications), since `add_init_script` runs on every new
  document in that page's context. It does NOT run on the document already loaded when
  `install_overlay()` is called, so `install_overlay()` also runs the same source once immediately
  via `page.evaluate()` — the two calls' overlap (a document that already has the overlay AND
  receives it again from `add_init_script` on the very next same-document evaluate) is guarded by
  overlay.js's own `window.__assistOverlayInstalled` check, which resets to falsy on every real
  navigation (each navigation gets a fresh `window`), so the guard only ever fires within the one
  document `install_overlay()` was called against.
- A caller who doesn't wire up one of the four bridge callbacks gets `_noop` exposed under that
  name rather than nothing at all: overlay.js's buttons call `window.__assist*` unconditionally
  when present, and a caller who left, say, `on_pause=None` most likely means "there is no pause
  behavior yet", not "crash the page's onclick handler when the button is clicked". A no-op default
  is the safer failure mode.
- `build_state()` is the one place that turns an ordered list of playbook steps (pipeline.jobs
  .apply.PlaybookStep, or an equivalent dict/duck-typed object with `.id`/`.title` or
  `["id"]`/`["title"]`) plus a `current_step_id` into the done/current/pending list the panel
  renders, so callers computing a fresh state for `render()` after each step transition never
  duplicate that walk themselves. It accepts either a dataclass-like object or a plain dict for
  each step so tests (and any future caller) aren't forced to construct real `PlaybookStep`s just
  to exercise this module — this module deliberately doesn't import pipeline.jobs.apply, keeping
  its own import graph independent of that module the same way pipeline.jobs.assist's package
  `__init__.py` keeps itself Playwright-free.
- `render()` takes the panel's already-shaped wire state (`{"steps": [...], "fields": [...],
  "needs_user": [...]}` — exactly what `overlay.js`'s `window.__assistRender()` expects) rather
  than the higher-level `(playbook_steps, current_step_id, fields, needs_user)` tuple
  `install_overlay()`'s `initial_state` argument carries: `render()` has no reason to know about
  playbook steps at all, so a caller who wants a done/current/pending recompute on a later call
  builds it via `build_state()` first, then passes the result straight through.
"""
from pathlib import Path
from typing import Any, Callable, Optional

from playwright.sync_api import Page

_JS_PATH = Path(__file__).parent / "overlay.js"

_FillCallback = Callable[[], Any]
_NextCallback = Callable[[], Any]
_EditAnswerCallback = Callable[[str, Any], Any]
_PauseCallback = Callable[[], Any]


def _noop(*_args: Any, **_kwargs: Any) -> None:
    """Default bridge callback for a button the caller didn't wire up. Does nothing, on purpose:
    see module docstring's "no-op default" design decision."""
    return None


def _step_attr(step: Any, key: str) -> Any:
    """The `key` value off a playbook step, whether it's a dataclass-like object (attribute access,
    e.g. pipeline.jobs.apply.PlaybookStep) or a plain dict (mapping access) — see build_state()'s
    docstring for why both are accepted."""
    if isinstance(step, dict):
        return step.get(key)
    return getattr(step, key, None)


def build_state(
    playbook_steps: list[Any],
    current_step_id: Optional[str],
    fields: list[dict],
    needs_user: list[dict],
) -> dict:
    """
    Builds the panel's wire-shaped state dict from an ordered playbook and the current step.

    Args:
        playbook_steps: The playbook's ordered steps (pipeline.jobs.apply.PlaybookStep instances,
            or dicts/objects exposing `id`/`title`), in playbook order.
        current_step_id: The `id` of the step currently in progress. Steps before it in
            `playbook_steps` are marked "done", that step "current", every step after it "pending".
            A value that matches no step's id (including None) leaves every step "pending".
        fields: The current step's filled fields, passed straight through into the returned state
            (see overlay.js's `renderFields()` for the shape: `field_id`, `label`, `value`,
            `source`, `confidence`, `selector`, `needs_user`).
        needs_user: The current "needs your attention" items, passed straight through (shape:
            `field_id`/`label`, `reason`).

    Returns:
        `{"steps": [{"id", "title", "status"}, ...], "fields": fields, "needs_user": needs_user}`,
        ready to hand to `render()` (or `install_overlay()`'s initial render).
    """
    steps: list[dict] = []
    seen_current = False
    for step in playbook_steps:
        step_id = _step_attr(step, "id")
        title = _step_attr(step, "title")
        if step_id == current_step_id:
            status = "current"
            seen_current = True
        elif seen_current:
            status = "pending"
        else:
            status = "done"
        steps.append({"id": step_id, "title": title, "status": status})
    return {"steps": steps, "fields": fields, "needs_user": needs_user}


def install_overlay(
    page: Page,
    playbook_steps: list[Any],
    initial_state: dict,
    *,
    on_fill: Optional[_FillCallback] = None,
    on_next: Optional[_NextCallback] = None,
    on_edit_answer: Optional[_EditAnswerCallback] = None,
    on_pause: Optional[_PauseCallback] = None,
) -> None:
    """
    Installs the checklist overlay on `page` and renders its initial state.

    Args:
        page: The Playwright `Page` to install the overlay into. May already be navigated, or
            still on its initial blank document — either way this renders into the currently
            loaded document immediately, and re-installs on every future navigation.
        playbook_steps: The playbook's ordered steps, per `build_state()`.
        initial_state: `{"current_step_id": Optional[str], "fields": list[dict], "needs_user":
            list[dict]}` — `fields`/`needs_user` default to `[]` when absent. Combined with
            `playbook_steps` via `build_state()` to produce the panel's first render.
        on_fill: Called with no arguments when the panel's "Fill page" button is clicked. Exposed
            as `window.__assistFillPage`. Defaults to a no-op.
        on_next: Called with no arguments when "Next step" is clicked. Exposed as
            `window.__assistNextStep`. Defaults to a no-op.
        on_edit_answer: Called with `(field_id: str, new_value: Any)` when "Edit answer" is
            clicked (overlay.js prompts the user for both). Exposed as
            `window.__assistEditAnswer`. Defaults to a no-op.
        on_pause: Called with no arguments when "Pause" is clicked. Exposed as
            `window.__assistPause`. Defaults to a no-op.

    Returns:
        None.

    Raises:
        playwright.sync_api.Error: propagated as-is from a failed `expose_function`/`evaluate`
            call (e.g. the page navigated away mid-install).
    """
    source = _JS_PATH.read_text(encoding="utf-8")

    page.expose_function("__assistFillPage", on_fill or _noop)
    page.expose_function("__assistNextStep", on_next or _noop)
    page.expose_function("__assistEditAnswer", on_edit_answer or _noop)
    page.expose_function("__assistPause", on_pause or _noop)

    # Future navigations (add_init_script runs on every new document); the currently loaded
    # document doesn't get add_init_script's copy, so it's also run once immediately below.
    page.add_init_script(source)
    page.evaluate(source)

    wire_state = build_state(
        playbook_steps,
        initial_state.get("current_step_id"),
        initial_state.get("fields") or [],
        initial_state.get("needs_user") or [],
    )
    render(page, wire_state)


def render(page: Page, state: dict) -> None:
    """
    Pushes an updated state dict into the already-installed overlay.

    Args:
        page: The Playwright `Page` `install_overlay()` was already called on.
        state: The panel's wire-shaped state, per `build_state()`'s return shape (`steps`,
            `fields`, `needs_user`) — build it with `build_state()` when the caller has a
            `current_step_id` rather than a precomputed step-status list.

    Returns:
        None.

    Raises:
        playwright.sync_api.Error: propagated as-is when the `evaluate()` call fails (e.g. the
            page navigated away, dropping `window.__assistRender` until the init script
            reinstalls it on the new document).
    """
    page.evaluate("(state) => { window.__assistRender(state); }", state)
