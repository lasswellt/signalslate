"""
Playwright field-filling for the desktop apply-assist runner (T-028, pipeline.jobs.assist), with a
hard code-level guard against ever clicking a final-submit control
(docs/_research/2026-09-21_jobs-collector.md "Apply Assist" > "Page reading and filling", steps
4-5, and its Guardrails: "Final submit, legal attestations and CAPTCHAs are always the user's.
Code-level guard, not just a prompt instruction.").

Design decisions:

- guarded_click() is the ONLY function in this module that ever calls Playwright's `.click()`.
  Every other public entry point that needs to click something (advance_step()) routes through it,
  so there is no click path anywhere in this module that skips the is_final_submit() check first.
- is_final_submit() errs toward false positives on purpose: a false positive here just means the
  runner leaves a Next/Continue-shaped control for the human to click manually (mildly annoying,
  always safe); a false negative would let the runner submit a real application (the one outcome
  this file exists to prevent). "Next"/"Continue"/"Save and continue" are the only text patterns
  carved out as explicitly NOT final-submit, checked before anything else — even a type="submit"
  Next button in a multi-step wizard (a common pattern: the button posts the current step, not the
  whole application) is not treated as final, because those three phrases carry no ambiguity about
  the user's intent to move within the flow rather than end it. Any other submit-like text
  ("submit", "submit application", "send application", "apply now") or a bare type="submit" is
  treated as final regardless of context.
- apply_proposals() resolves each field's element via the field's own `selector` (from
  pipeline.jobs.assist.extract.py), namespaced back to the right frame using that same module's
  frame-namespacing scheme ("main:<id>" -> page.main_frame, "frame_<i>:<id>" -> page.frames[i]) so
  this module never needs its own copy of that logic.
- Verify-after-fill: React-controlled inputs sometimes silently reject a programmatic `.fill()`
  (the DOM value never actually changes), so every text-like fill re-reads `.input_value()` and,
  on a mismatch, retries exactly once via keystroke simulation (`press_sequentially()`) before
  giving up on that field — matching the research doc's step 4. File inputs always use
  `set_input_files()` (never a fake drag-drop or keystroke sequence), verified by re-reading the
  element's `.files.length` rather than `.input_value()` (browsers refuse to expose a file input's
  real value to script).
- One field's failure never costs the batch: each field's fill is isolated in its own try/except
  (this codebase's general "one item's failure doesn't cost the whole batch" convention, e.g.
  pipeline.domains.intel's per-source isolation), and apply_proposals() returns a per-field result
  list rather than raising.
- _sleep is a module-level indirection over time.sleep (pipeline.domains.intel's pattern), so tests
  don't actually wait out the human-ish pacing between fills.
"""
import random
import re
import time
from typing import Any, Optional

from playwright.sync_api import Page

_sleep = time.sleep

# --- Final-submit guard ---------------------------------------------------------------------------

# Explicitly NOT a final-submit control, even when the underlying markup is a <button
# type="submit"> that only posts the current step (a common multi-step-wizard pattern): checked
# before anything else, so it wins over a submit `type`.
_NON_FINAL_TEXT_RE = re.compile(r"^(next( step)?|continue|save\s+and\s+continue)$", re.IGNORECASE)

# Submit-like phrasing: treated as final regardless of the element's `type`.
_FINAL_TEXT_RE = re.compile(r"submit\s+application|send\s+application|apply\s+now|^submit$", re.IGNORECASE)


class SubmitGuardError(Exception):
    """
    Raised whenever code in this module is about to click something that looks like a final
    application-submit control. This is the enforcement mechanism for "never click final submit"
    (see module docstring) — not a warning: the click never happens.
    """


def is_final_submit(locator_info: dict) -> bool:
    """
    Whether a clickable element looks like a final application-submit control.

    Args:
        locator_info: Description of one clickable element, at minimum
            `{"text": str, "type": Optional[str], "selector": str}` — `text` is the element's
            visible text/aria-label, `type` is its HTML `type` attribute (e.g. "submit", "button",
            or None/"" when it has none), `selector` a CSS selector re-locating it (unused by this
            function; carried here so callers can pass the same dict straight to guarded_click()).

    Returns:
        True when the element should never be auto-clicked by this runner: its `type` is "submit"
        (a real HTML submit button), or its text/aria-label matches a final-submit phrase
        ("submit", "submit application", "send application", "apply now"). False for Next/
        Continue/Save-and-continue-style controls, checked first and returned as non-final even
        when `type == "submit"` (see module docstring). Ambiguous submit-like text is deliberately
        treated as final: a false positive here is always safe, a false negative is not.
    """
    text = " ".join((locator_info.get("text") or "").split()).strip()
    element_type = (locator_info.get("type") or "").strip().lower()

    if _NON_FINAL_TEXT_RE.match(text):
        return False
    if element_type == "submit":
        return True
    if _FINAL_TEXT_RE.search(text):
        return True
    return False


def guarded_click(page_or_frame: Any, locator_info: dict) -> None:
    """
    Clicks one element, after refusing to do so if it looks like a final-submit control.

    This is the ONLY function in this module that calls Playwright's `.click()` — every other
    click-shaped entry point (advance_step()) routes through this one, so nothing in this module
    can click an arbitrary selector without the is_final_submit() check running first.

    Args:
        page_or_frame: A Playwright `Page` or `Frame` exposing `.locator(selector)`.
        locator_info: The element description, per is_final_submit()'s docstring; `selector` is
            required here (used to locate the element to click).

    Returns:
        None.

    Raises:
        SubmitGuardError: is_final_submit(locator_info) is True. Raised before any Playwright call
            runs — the click never reaches the real page.
        playwright.sync_api.Error: propagated as-is when the element cannot be found/clicked.
    """
    if is_final_submit(locator_info):
        raise SubmitGuardError(f"refused to click a final-submit control: {locator_info!r}")
    page_or_frame.locator(locator_info["selector"]).click()


def advance_step(page_or_frame: Any, locator_info: dict) -> None:
    """
    Clicks a Next/Continue-style control to move to the next step of a multi-step form.

    A thin, intent-naming wrapper over guarded_click(): since guarded_click() already refuses a
    final-submit target, this is the same guard, used where the caller means "move forward in the
    flow" rather than "click this specific thing" — it can never bypass the guard.

    Args:
        page_or_frame: A Playwright `Page` or `Frame` exposing `.locator(selector)`.
        locator_info: The Next/Continue control's description, per is_final_submit()'s docstring.

    Returns:
        None.

    Raises:
        SubmitGuardError: locator_info actually looks like a final-submit control (defensive: a
            caller passed the wrong element).
        playwright.sync_api.Error: propagated as-is when the element cannot be found/clicked.
    """
    guarded_click(page_or_frame, locator_info)


# --- Filling ---------------------------------------------------------------------------------------


def _resolve_frame(page: Page, field_id: str) -> Any:
    """
    The Playwright `Page`/`Frame` a namespaced field_id's element actually lives in, per
    pipeline.jobs.assist.extract's frame-namespacing scheme ("main:<id>" -> the page's main frame,
    "frame_<i>:<id>" -> page.frames[i]).

    Args:
        page: The Playwright `Page` the field was extracted from.
        field_id: A namespaced field_id, e.g. "main:full_name" or "frame_2:referral_name".

    Returns:
        page.main_frame for a "main:" id, page.frames[i] for a "frame_<i>:" id, falling back to
        page.main_frame if the index is out of range or unparsable rather than raising — a stale
        field_id from a page that has since navigated is that field's own fill failure, not this
        function's problem to diagnose.
    """
    prefix, _, _ = field_id.partition(":")
    if prefix.startswith("frame_"):
        try:
            index = int(prefix[len("frame_"):])
        except ValueError:
            return page.main_frame
        frames = page.frames
        if 0 <= index < len(frames):
            return frames[index]
    return page.main_frame


def _proposal_get(proposal: Any, key: str) -> Any:
    """The `key` value off a proposal, whether it is a mapping.FieldProposal or a plain dict (this
    module's public entry point accepts either, per apply_proposals()'s signature)."""
    if isinstance(proposal, dict):
        return proposal.get(key)
    return getattr(proposal, key, None)


def _fill_text(locator: Any, value: Any) -> tuple[bool, Optional[str]]:
    """Fills a text-like input/textarea, retrying once via keystroke simulation on a value
    mismatch (a React-controlled input silently rejecting the programmatic `.fill()`)."""
    intended = "" if value is None else str(value)
    locator.fill(intended)
    actual = locator.input_value()
    if actual == intended:
        return True, None
    locator.fill("")
    locator.press_sequentially(intended)
    actual = locator.input_value()
    if actual == intended:
        return True, None
    return False, f"value mismatch after fill+retry: expected {intended!r}, got {actual!r}"


def _fill_select(locator: Any, field: dict, value: Any) -> tuple[bool, Optional[str]]:
    """Selects an option by value, falling back to matching by visible option text when `value`
    doesn't match any option's `value` (a proposal may carry the option's label, not its value)."""
    options = field.get("options") or []
    intended = "" if value is None else str(value)
    option_values = {str(opt.get("value", "")) for opt in options}
    target_value = intended
    if intended not in option_values:
        for opt in options:
            if str(opt.get("text", "")) == intended:
                target_value = str(opt.get("value", ""))
                break
    locator.select_option(value=target_value)
    actual = locator.input_value()
    if actual == target_value:
        return True, None
    return False, f"select mismatch: expected {target_value!r}, got {actual!r}"


def _fill_checkbox(locator: Any, value: Any) -> tuple[bool, Optional[str]]:
    """Checks/unchecks per `value`'s truthiness."""
    intended = bool(value)
    if intended:
        locator.check()
    else:
        locator.uncheck()
    actual = locator.is_checked()
    if actual == intended:
        return True, None
    return False, f"checkbox state mismatch: expected {intended}, got {actual}"


def _fill_file(locator: Any, value: Any) -> tuple[bool, Optional[str]]:
    """Attaches a file via `set_input_files()` — never a fake drag-drop or keystroke sequence
    (research doc: "set_input_files() handles resume and cover-letter uploads reliably"). Verified
    via the element's own `.files.length` rather than `.input_value()`, which browsers refuse to
    expose for file inputs."""
    locator.set_input_files(value)
    attached = locator.evaluate("el => el.files.length")
    if attached:
        return True, None
    return False, "file input has no attached files after set_input_files"


def _fill_field(frame: Any, field: dict, value: Any) -> tuple[bool, Optional[str]]:
    """Fills one field per its extracted `type`, dispatching to the type-specific helper above."""
    locator = frame.locator(field["selector"])
    field_type = (field.get("type") or "").lower()
    if field_type == "checkbox":
        return _fill_checkbox(locator, value)
    if field_type == "select":
        return _fill_select(locator, field, value)
    if field_type == "file":
        return _fill_file(locator, value)
    return _fill_text(locator, value)


def apply_proposals(
    page: Page,
    fields: list[dict],
    proposals: list[Any],
    *,
    delay_range: tuple[float, float] = (0.05, 0.2),
) -> list[dict]:
    """
    Fills every proposal that doesn't need the user, verifying each fill actually stuck.

    Args:
        page: The Playwright `Page` the fields were extracted from (pipeline.jobs.assist.extract
            .extract_fields()'s frame-namespacing scheme routes each field back to the right
            frame).
        fields: The field descriptors extract_fields() returned (or an equivalent plain-dict list
            in tests), keyed by their namespaced `field_id`.
        proposals: One entry per field to consider, each either a mapping.FieldProposal or an
            equivalent dict, using its `field_id`, `value`, `needs_user` keys.
        delay_range: Seconds to sleep between fills, drawn from `random.uniform(*delay_range)` for
            human-ish pacing (research doc: "human-ish pacing").

    Returns:
        One `{"field_id": str, "ok": bool, "error": Optional[str]}` per proposal actually
        attempted. A proposal with `needs_user=True`, or a `field_id` absent from `fields`, is
        skipped entirely (no entry in the result list) — those are the human's job or a stale/
        invalid proposal, respectively. A field whose fill raised, or whose verify-after-fill still
        mismatched after the retry, gets `ok=False` with `error` set; one field's failure never
        stops the rest.
    """
    fields_by_id = {field["field_id"]: field for field in fields}
    results: list[dict] = []

    for proposal in proposals:
        field_id = _proposal_get(proposal, "field_id")
        if _proposal_get(proposal, "needs_user"):
            continue
        field = fields_by_id.get(field_id)
        if field is None:
            continue

        _sleep(random.uniform(*delay_range))
        try:
            frame = _resolve_frame(page, field_id)
            ok, error = _fill_field(frame, field, _proposal_get(proposal, "value"))
        except Exception as exc:
            # One field's failure isolated from the batch; class name + message is fine here (see
            # module docstring) since this is desktop-side, page-structure detail, not a
            # server-side error that could leak request/response internals.
            ok, error = False, f"{type(exc).__name__}: {exc}"
        results.append({"field_id": field_id, "ok": ok, "error": error})

    return results
