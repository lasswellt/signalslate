"""
In-page field extractor for the desktop apply-assist runner (T-026, pipeline.jobs.assist).

Runs extract.js's `extractFields()` via Playwright's `page.evaluate()` / `frame.evaluate()`
against a page's main frame and every child frame (Greenhouse embeds are iframes, per
docs/_research/2026-09-21_jobs-collector.md "Apply Assist" > "Page reading and filling", step 1
"Extract"), then combines the frames' field lists into one.

extract.js is loaded from the sibling file rather than inlined as a Python string literal, so the
two files stay independently readable/editable (this task's own instruction) — the only
Python-side work here is running that source per frame and namespacing the results.

Frame namespacing: extract.js's `field_id` is only unique *within* the frame it ran in (an id or
name attribute has no cross-frame uniqueness guarantee), so this module prefixes it before adding
a frame's fields to the combined list:

- main frame -> "main:<raw field_id>"
- any other frame -> "frame_<i>:<raw field_id>", where <i> is that frame's own index in
  `page.frames` (stable for the life of the page, unlike an ad-hoc counter).

This submodule imports Playwright directly (unlike pipeline.jobs.assist's package `__init__.py`,
which stays Playwright-free per its own docstring) — this task's new submodules are the ones
allowed to.
"""
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

_JS_PATH = Path(__file__).parent / "extract.js"


def _evaluate_frame(frame: Any) -> list[dict]:
    """
    Runs extract.js's `extractFields()` against one frame and returns its raw field list.

    Args:
        frame: A Playwright `Page` or `Frame` object exposing `.evaluate(expression)`.

    Returns:
        The list of field dicts `extractFields()` returned for this frame, with each dict's
        `field_id` still the raw, un-namespaced value from extract.js.

    Raises:
        playwright.sync_api.Error: propagated as-is when the evaluate call itself fails (e.g. the
            frame detached or navigated away mid-read).
    """
    source = _JS_PATH.read_text(encoding="utf-8")
    return frame.evaluate(f"() => {{ {source}\nreturn extractFields(); }}")


def extract_fields(page: Page) -> list[dict]:
    """
    Extracts every fillable field on a page, across its main frame and every child frame.

    Args:
        page: The Playwright `Page` to read (already navigated to the form).

    Returns:
        A combined list of field dicts (`field_id`, `label`, `type`, `required`, `options`,
        `current_value`, `section`, `selector` — see extract.js's `extractFields()` for the
        per-field shape), each `field_id` namespaced by its frame so fields from different frames
        never collide (see module docstring for the exact scheme).

    Raises:
        playwright.sync_api.Error: propagated as-is from a failed `.evaluate()` call on any frame;
            not caught here — a human is watching this runner live (see pipeline.jobs.assist
            .client's module docstring for the same "surface failures, don't swallow them" rule),
            so a read that silently drops a frame's fields would be worse than one that raises.
    """
    fields: list[dict] = []
    main_frame = page.main_frame
    for raw in _evaluate_frame(page):
        raw["field_id"] = f"main:{raw['field_id']}"
        fields.append(raw)
    for index, frame in enumerate(page.frames):
        if frame is main_frame:
            continue
        for raw in _evaluate_frame(frame):
            raw["field_id"] = f"frame_{index}:{raw['field_id']}"
            fields.append(raw)
    return fields
