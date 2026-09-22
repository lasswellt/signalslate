"""
Tests for pipeline.jobs.assist.fill (T-028): applying field proposals via Playwright, and the hard
final-submit guard.

Launches a real headless Chromium via playwright.sync_api.sync_playwright() — no mocking of the
browser, DOM or Playwright API — loads tests/fixtures/jobs_forms/multistep_form.html via file://,
and drives it exactly the way the desktop runner would: extract_fields() reads the live page,
apply_proposals() fills it, advance_step() clicks "Next", and guarded_click() is exercised directly
against the fixture's real "Submit Application" button to prove the click never reaches it (the
fixture's own window.__submitted flag, settable only by that button's real click handler, is the
oracle for that last claim).
"""
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs.assist.extract import extract_fields  # noqa: E402
from pipeline.jobs.assist.fill import (  # noqa: E402
    SubmitGuardError,
    advance_step,
    apply_proposals,
    guarded_click,
    is_final_submit,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "jobs_forms" / "multistep_form.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    try:
        page.goto(f"file://{FIXTURE_PATH}")
        yield page
    finally:
        page.close()


def _by_id(fields: list[dict], field_id: str) -> dict:
    matches = [field for field in fields if field["field_id"] == field_id]
    assert len(matches) == 1, f"expected exactly one field with field_id={field_id!r}, got {matches}"
    return matches[0]


# --- is_final_submit: unit-level table test -------------------------------------------------------


@pytest.mark.parametrize(
    "locator_info,expected",
    [
        ({"text": "Submit", "type": "submit", "selector": "#s"}, True),
        ({"text": "Submit Application", "type": "button", "selector": "#sa"}, True),
        ({"text": "Send Application", "type": "button", "selector": "#send"}, True),
        ({"text": "Apply Now", "type": "button", "selector": "#an"}, True),
        ({"text": "Next", "type": "button", "selector": "#n"}, False),
        ({"text": "Continue", "type": "button", "selector": "#c"}, False),
        ({"text": "Save and Continue", "type": "submit", "selector": "#sc"}, False),
        ({"text": "Cancel", "type": "button", "selector": "#x"}, False),
        ({"text": "", "type": None, "selector": "#no-type"}, False),
    ],
)
def test_is_final_submit_table(locator_info, expected):
    assert is_final_submit(locator_info) is expected


# --- apply_proposals --------------------------------------------------------------------------------


def test_apply_proposals_fills_text_fields_and_file(page, tmp_path):
    fields = extract_fields(page)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 fake resume bytes")

    proposals = [
        {"field_id": "main:full_name", "value": "Jane Doe", "confidence": 0.95, "source": "profile", "needs_user": False},
        {"field_id": "main:email", "value": "jane@example.com", "confidence": 0.95, "source": "profile", "needs_user": False},
        {"field_id": "main:resume", "value": str(resume_path), "confidence": 0.95, "source": "resume", "needs_user": False},
        # needs_user=True: must be skipped, never filled, never in the result list.
        {"field_id": "main:full_name", "value": "Should Not Apply", "confidence": 0.0, "source": "generated", "needs_user": True},
        # No matching field: must be silently skipped, not an error.
        {"field_id": "main:does_not_exist", "value": "x", "confidence": 0.5, "source": "generated", "needs_user": False},
    ]

    results = apply_proposals(page, fields, proposals, delay_range=(0.0, 0.0))

    result_by_id = {r["field_id"]: r for r in results}
    assert set(result_by_id) == {"main:full_name", "main:email", "main:resume"}
    for result in results:
        assert result["ok"] is True, result
        assert result["error"] is None

    assert page.locator("#full_name").input_value() == "Jane Doe"
    assert page.locator("#email").input_value() == "jane@example.com"
    attached_name = page.locator("#resume").evaluate("el => el.files.length ? el.files[0].name : null")
    assert attached_name == "resume.pdf"


def test_apply_proposals_needs_user_is_skipped(page):
    fields = extract_fields(page)
    proposals = [
        {"field_id": "main:full_name", "value": "Should Not Fill", "confidence": 0.0, "source": "generated", "needs_user": True}
    ]
    results = apply_proposals(page, fields, proposals, delay_range=(0.0, 0.0))
    assert results == []
    assert page.locator("#full_name").input_value() == ""


# --- advance_step / guarded_click -------------------------------------------------------------------


def test_advance_step_clicks_next(page):
    assert page.locator("#step-2").is_visible() is False
    advance_step(page, {"text": "Next", "type": "button", "selector": "#next_btn"})
    assert page.locator("#step-2").is_visible() is True
    assert page.evaluate("window.__submitted") is False


def test_guarded_click_refuses_final_submit(page):
    # Move to the review step first, same as the real runner would before ever considering
    # clicking "Submit Application".
    advance_step(page, {"text": "Next", "type": "button", "selector": "#next_btn"})
    assert page.locator("#submit_btn").is_visible() is True

    with pytest.raises(SubmitGuardError):
        guarded_click(page, {"text": "Submit Application", "type": "submit", "selector": "#submit_btn"})

    # The critical assertion: the guard raised BEFORE any real click reached the button, so the
    # fixture's own click handler (the only thing that can set this flag) never ran.
    assert page.evaluate("window.__submitted") is False


def test_guarded_click_allows_non_final_click(page):
    # guarded_click's non-final path still performs a real click (proves the guard doesn't just
    # always refuse): reuses the Next button as an ordinary, non-final target.
    assert page.locator("#step-2").is_visible() is False
    guarded_click(page, {"text": "Next", "type": "button", "selector": "#next_btn"})
    assert page.locator("#step-2").is_visible() is True
