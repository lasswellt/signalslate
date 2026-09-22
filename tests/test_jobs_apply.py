"""
Tests for pipeline.jobs.apply: per-ATS playbooks and the guarded JobApplication state machine.

No DB session needed: transition() only mutates the .status attribute of whatever JobApplication
instance it's given, so these tests construct rows in memory (mirrors tests/test_job_apply_tables.py's
fixtures conceptually, but skips the temp-engine plumbing since nothing here touches the DB).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import JobApplication  # noqa: E402
from pipeline.jobs.apply import (  # noqa: E402
    PLAYBOOKS,
    PlaybookStep,
    get_playbook,
    transition,
)

_ALL_STEP_KINDS = {"auth", "form", "upload", "questions", "disclosures", "review"}


def _application(status: str) -> JobApplication:
    return JobApplication(posting_id=1, status=status)


def test_playbooks_keyed_by_ats_kind_with_generic_fallback():
    assert "workday" in PLAYBOOKS
    assert "_generic" in PLAYBOOKS
    for steps in PLAYBOOKS.values():
        assert steps, "every playbook has at least one step"
        for step in steps:
            assert isinstance(step, PlaybookStep)
            assert step.kind in _ALL_STEP_KINDS


def test_workday_playbook_matches_research_doc_order_and_flags():
    steps = get_playbook("workday")
    assert [step.id for step in steps] == [
        "sign_in",
        "verify_email",
        "my_information",
        "my_experience",
        "application_questions",
        "voluntary_disclosures",
        "self_identify",
        "review",
    ]

    by_id = {step.id: step for step in steps}
    assert by_id["sign_in"].kind == "auth" and by_id["sign_in"].user_only is True
    assert by_id["verify_email"].kind == "auth" and by_id["verify_email"].user_only is True
    assert by_id["my_information"].kind == "form" and by_id["my_information"].user_only is False
    assert by_id["my_experience"].kind == "form" and by_id["my_experience"].user_only is False
    assert by_id["application_questions"].kind == "questions"
    assert by_id["application_questions"].user_only is False
    assert by_id["voluntary_disclosures"].kind == "disclosures"
    assert by_id["voluntary_disclosures"].user_only is True
    assert by_id["self_identify"].kind == "disclosures" and by_id["self_identify"].user_only is True
    assert by_id["review"].kind == "review" and by_id["review"].user_only is True


def test_generic_playbook_is_one_form_step_then_review():
    steps = get_playbook("_generic")
    assert [(step.kind, step.user_only) for step in steps] == [("form", False), ("review", True)]


@pytest.mark.parametrize("ats_kind", ["greenhouse", "lever", "ashby", "smartrecruiters", "bogus-kind", ""])
def test_unlisted_ats_kinds_fall_back_to_generic(ats_kind):
    assert get_playbook(ats_kind) == PLAYBOOKS["_generic"]


def test_playbook_step_is_frozen():
    step = get_playbook("workday")[0]
    with pytest.raises(Exception):
        step.title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "start,end",
    [
        ("saved", "preparing"),
        ("preparing", "ready"),
        ("ready", "submitted"),
        ("submitted", "interviewing"),
        ("submitted", "rejected"),
        ("interviewing", "rejected"),
        ("interviewing", "closed"),
        ("saved", "withdrawn"),
        ("preparing", "withdrawn"),
        ("ready", "withdrawn"),
        ("submitted", "withdrawn"),
        ("interviewing", "withdrawn"),
    ],
)
def test_legal_transitions_mutate_status(start, end):
    application = _application(start)
    result = transition(application, end)
    assert application.status == end
    assert result is application


@pytest.mark.parametrize(
    "start,end",
    [
        ("saved", "submitted"),  # skips preparing/ready
        ("saved", "ready"),
        ("preparing", "submitted"),
        ("preparing", "interviewing"),
        ("ready", "interviewing"),
        ("submitted", "preparing"),
        ("rejected", "saved"),
        ("closed", "interviewing"),
        ("withdrawn", "saved"),
        ("saved", "saved"),
        ("saved", "nonexistent-status"),
    ],
)
def test_illegal_transitions_raise_value_error_naming_both_states(start, end):
    application = _application(start)
    with pytest.raises(ValueError) as excinfo:
        transition(application, end)
    assert start in str(excinfo.value)
    assert end in str(excinfo.value)
    assert application.status == start  # unchanged on rejection


def test_terminal_statuses_allow_no_further_transition():
    for terminal in ("rejected", "closed", "withdrawn"):
        application = _application(terminal)
        for candidate in ("saved", "preparing", "ready", "submitted", "interviewing", "rejected", "closed", "withdrawn"):
            if candidate == terminal:
                continue
            with pytest.raises(ValueError):
                transition(application, candidate)
