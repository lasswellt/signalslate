"""
Tests for pipeline.jobs.assist.extract.extract_fields (T-026).

Launches a real headless Chromium via playwright.sync_api.sync_playwright() — no mocking of the
browser or DOM — loads tests/fixtures/jobs_forms/greenhouse_form.html via file://, and asserts the
extracted fields match what that fixture actually contains: field count, label resolution for both
the `<label for>` and `aria-label` cases, the `<select>`'s options, the required checkbox, and a
field inside the fixture's srcdoc `<iframe>` (exercising the "section" / frame-namespacing path).
"""
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs.assist.extract import extract_fields  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "jobs_forms" / "greenhouse_form.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def fields(browser) -> list[dict]:
    page = browser.new_page()
    try:
        page.goto(f"file://{FIXTURE_PATH}")
        page.frame_locator("#embed").locator("#referral_name").wait_for(state="attached")
        return extract_fields(page)
    finally:
        page.close()


def _by_id(fields: list[dict], field_id: str) -> dict:
    matches = [field for field in fields if field["field_id"] == field_id]
    assert len(matches) == 1, f"expected exactly one field with field_id={field_id!r}, got {matches}"
    return matches[0]


def test_field_count(fields):
    # main frame: full_name, email, phone, source (select), cover_note (textarea), agree
    # (checkbox) = 6; embedded iframe frame: referral_name = 1.
    assert len(fields) == 7


def test_label_for_case(fields):
    field = _by_id(fields, "main:full_name")
    assert field["label"] == "Full Name"
    assert field["type"] == "text"
    assert field["required"] is True
    assert field["section"] == "Contact Information"


def test_aria_label_case(fields):
    field = _by_id(fields, "main:email")
    assert field["label"] == "Email Address"
    assert field["type"] == "email"
    assert field["required"] is True


def test_nearby_text_case(fields):
    field = _by_id(fields, "main:phone")
    assert field["label"] == "Phone Number"
    assert field["type"] == "tel"
    assert field["required"] is False


def test_select_options(fields):
    field = _by_id(fields, "main:source")
    assert field["type"] == "select"
    assert field["label"] == "How did you hear about us?"
    assert field["options"] == [
        {"value": "", "text": "Select one"},
        {"value": "linkedin", "text": "LinkedIn"},
        {"value": "referral", "text": "Referral"},
        {"value": "job_board", "text": "Job Board"},
    ]


def test_textarea(fields):
    field = _by_id(fields, "main:cover_note")
    assert field["type"] == "textarea"
    assert field["label"] == "Why do you want to work here?"


def test_required_checkbox(fields):
    field = _by_id(fields, "main:agree")
    assert field["type"] == "checkbox"
    assert field["required"] is True
    assert field["label"] == "I agree to the terms and privacy policy"
    assert field["current_value"] is False
    # A lone checkbox (no sibling sharing its name) is not a "group" -> options stays empty.
    assert field["options"] == []


def test_iframe_field_is_namespaced(fields):
    iframe_fields = [f for f in fields if f["field_id"].startswith("frame_") and f["field_id"].endswith(":referral_name")]
    assert len(iframe_fields) == 1
    field = iframe_fields[0]
    assert field["label"] == "Referral Name"
    assert field["type"] == "text"


def test_selectors_are_unique(fields):
    selectors = [field["selector"] for field in fields]
    assert len(selectors) == len(set(selectors))
