"""
Tests for pipeline.jobs.assist.overlay (T-029): the shadow-DOM checklist panel and its
Fill/Next/Edit answer/Pause bridge to Python.

Launches a real headless Chromium via playwright.sync_api.sync_playwright() — no mocking of the
browser, DOM or Playwright API. Uses page.set_content() for a minimal in-page target element
(this task's SCOPE_FILES carries no fixture file of its own), then install_overlay() with plain
Python functions that record their own calls as the callbacks; assertions check calls that
actually happened, not a mocked expectation. Playwright's locators pierce open shadow roots
automatically, so buttons inside the panel's shadow root are reached with ordinary locators.

Bridge calls are async from the page's perspective (Playwright's expose_function() returns them as
Promises); overlay.js records the last-completed action on window.__assistLastAction once each
bridge call's Promise resolves, and each button test waits on that instead of racing the Python
round-trip.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs.assist.overlay import build_state, install_overlay, render  # noqa: E402


@dataclass(frozen=True)
class _Step:
    id: str
    title: str
    kind: str = "form"
    user_only: bool = False


PLAYBOOK = [
    _Step(id="my_info", title="My Information"),
    _Step(id="my_experience", title="My Experience"),
    _Step(id="review", title="Review"),
]

INITIAL_STATE = {
    "current_step_id": "my_experience",
    "fields": [
        {
            "field_id": "main:full_name",
            "label": "Full Name",
            "value": "Jane Doe",
            "source": "profile",
            "confidence": 0.95,
            "selector": "#full_name",
            "needs_user": False,
        },
        {
            "field_id": "main:cover_note",
            "label": "Cover Note",
            "value": "Generated cover note text",
            "source": "generated",
            "confidence": 0.4,
            # Reuses the same page element so the low-confidence highlight is easy to assert on.
            "selector": "#full_name",
            "needs_user": False,
        },
    ],
    "needs_user": [
        {"field_id": "main:agree", "label": "I certify the above is true", "reason": "attestation"},
    ],
}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def calls():
    return {"fill": [], "next": [], "edit": [], "pause": []}


@pytest.fixture
def page(browser, calls):
    page = browser.new_page()
    try:
        page.set_content('<html><body><input id="full_name" value=""></body></html>')

        def on_fill():
            calls["fill"].append(True)

        def on_next():
            calls["next"].append(True)

        def on_edit_answer(field_id, new_value):
            calls["edit"].append((field_id, new_value))

        def on_pause():
            calls["pause"].append(True)

        install_overlay(
            page,
            PLAYBOOK,
            INITIAL_STATE,
            on_fill=on_fill,
            on_next=on_next,
            on_edit_answer=on_edit_answer,
            on_pause=on_pause,
        )
        yield page
    finally:
        page.close()


def _panel_text(page) -> str:
    return page.locator("#__assist-overlay-host").locator("div.assist-panel").inner_text()


def _click_and_wait(page, button_text: str, action: str) -> None:
    page.evaluate("window.__assistLastAction = null")
    page.locator("button", has_text=button_text).click()
    page.wait_for_function("window.__assistLastAction === " + repr(action))


# --- Install + render content ------------------------------------------------------------------


def test_shadow_host_exists(page):
    assert page.locator("#__assist-overlay-host").count() == 1


def test_panel_shows_steps_and_fields(page):
    text = _panel_text(page)
    assert "My Information" in text
    assert "My Experience" in text
    assert "Review" in text
    assert "Full Name" in text
    assert "Jane Doe" in text
    assert "Cover Note" in text


def test_panel_shows_needs_user_item(page):
    text = _panel_text(page)
    assert "I certify the above is true" in text


def test_low_confidence_field_is_highlighted_in_panel_and_on_page(page):
    low_confidence_item = page.locator("#__assist-overlay-host").locator(
        "li.assist-field--low-confidence"
    )
    assert low_confidence_item.count() == 1
    assert "Cover Note" in low_confidence_item.inner_text()

    outline = page.locator("#full_name").evaluate("el => el.style.outline")
    assert outline and "solid" in outline


def test_build_state_step_statuses():
    state = build_state(PLAYBOOK, "my_experience", [], [])
    statuses = {step["id"]: step["status"] for step in state["steps"]}
    assert statuses == {"my_info": "done", "my_experience": "current", "review": "pending"}


def test_render_pushes_updated_state(page):
    new_state = build_state(
        PLAYBOOK,
        "review",
        [
            {
                "field_id": "main:full_name",
                "label": "Full Name",
                "value": "Jane Doe",
                "source": "profile",
                "confidence": 0.99,
                "selector": "#full_name",
                "needs_user": False,
            }
        ],
        [],
    )
    render(page, new_state)
    text = _panel_text(page)
    assert "Full Name" in text
    # The stale, low-confidence Cover Note field from the initial render is gone after re-render.
    assert "Cover Note" not in text


# --- Button -> Python bridge ---------------------------------------------------------------------


def test_fill_button_invokes_callback(page, calls):
    _click_and_wait(page, "Fill page", "fill")
    assert calls["fill"] == [True]


def test_next_button_invokes_callback(page, calls):
    _click_and_wait(page, "Next step", "next")
    assert calls["next"] == [True]


def test_pause_button_invokes_callback(page, calls):
    _click_and_wait(page, "Pause", "pause")
    assert calls["pause"] == [True]


def test_edit_answer_button_invokes_callback_with_field_and_value(page, calls):
    responses = iter(["main:cover_note", "Updated value"])

    def handle_dialog(dialog):
        dialog.accept(next(responses))

    page.on("dialog", handle_dialog)
    _click_and_wait(page, "Edit answer", "edit")
    assert calls["edit"] == [("main:cover_note", "Updated value")]
