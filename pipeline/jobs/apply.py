"""
Per-ATS apply playbooks and the guarded JobApplication state machine (research doc "Apply Assist" §
"Checklist overlay", Recommendation phase 2).

Design decisions:

- PLAYBOOKS is a static dict, not a callable-per-kind registry like pipeline.jobs.ats: a playbook is
  just an ordered list of steps (no per-board fetch, no probe/list_postings behavior), so a plain
  mapping is the smallest thing that satisfies get_playbook(). "_generic" is the fallback key rather
  than a bare default parameter, so a caller inspecting PLAYBOOKS directly (e.g. the checklist-overlay
  UI listing every known playbook) sees it too.
- Every ATS other than Workday shares "_generic" (research doc: "The generic playbook for unknown
  forms is one step per page") rather than getting its own entry — Greenhouse/Lever/Ashby embed
  forms with stable field names the assist fills deterministically, but they don't have Workday's
  named multi-page account/disclosure flow to model, so a bespoke playbook per kind would just repeat
  "_generic" under a different key. AtsAdapter.apply_mode == "account_per_tenant" is what makes
  Workday different: signing into a per-tenant account is itself a playbook step, unlike
  "hosted_form" ATSes where the runner is never asked to authenticate.
- user_only marks the steps the runner must never act on unattended: CAPTCHA/human-judgment account
  creation and email verification, the EEO-adjacent Voluntary Disclosures/Self Identify pages (this
  codebase's eeo_answers convention defaults to decline-to-answer — a human confirms those, the
  assist never guesses), and Review, which is the final human gate before Submit (§Apply Assist
  guardrails: "Never click final submit"). My Information/My Experience/Application Questions are
  ordinary form-filling the assist can complete from JobsProfile/AnswerBank, so user_only=False.
- transition() takes an explicit `_ALLOWED_TRANSITIONS: dict[str, set[str]]` map rather than deriving
  legal moves from field order, mirroring this codebase's guarded-state-machine convention (a
  caller-input rejection raises ValueError, not a silent no-op or an assert). It mutates and returns
  the same JobApplication instance rather than a copy — callers already hold the ORM row and commit
  it themselves (see pipeline/domains/purchase.py's DomainPurchase status writes), so a copy would
  just be discarded.
"""
from dataclasses import dataclass

from pipeline.db import JobApplication

# kind: auth | form | upload | questions | disclosures | review
_STEP_KINDS = frozenset({"auth", "form", "upload", "questions", "disclosures", "review"})

_GENERIC_KIND = "_generic"


@dataclass(frozen=True)
class PlaybookStep:
    """One step of an ATS apply playbook, as the checklist overlay renders it.

    user_only=True marks a step the runner must leave entirely to the human (CAPTCHAs, legal
    attestations, final submit) — the assist may still show what it filled, but it never advances
    or submits that step on its own.
    """
    id: str
    title: str
    kind: str  # one of _STEP_KINDS
    user_only: bool


# Workday: apply_mode == "account_per_tenant" (pipeline/jobs/ats/__init__.py AtsAdapter.apply_mode).
# Step order/titles per research doc §Checklist overlay, verbatim:
# "sign in / create account -> verify email -> My Information -> My Experience (resume upload +
# correct the parsed history) -> Application Questions -> Voluntary Disclosures -> Self Identify ->
# Review".
_WORKDAY_PLAYBOOK: list[PlaybookStep] = [
    PlaybookStep(id="sign_in", title="Sign in / create account", kind="auth", user_only=True),
    PlaybookStep(id="verify_email", title="Verify email", kind="auth", user_only=True),
    PlaybookStep(id="my_information", title="My Information", kind="form", user_only=False),
    PlaybookStep(id="my_experience", title="My Experience", kind="form", user_only=False),
    PlaybookStep(id="application_questions", title="Application Questions", kind="questions", user_only=False),
    PlaybookStep(id="voluntary_disclosures", title="Voluntary Disclosures", kind="disclosures", user_only=True),
    PlaybookStep(id="self_identify", title="Self Identify", kind="disclosures", user_only=True),
    PlaybookStep(id="review", title="Review", kind="review", user_only=True),
]

# Every ats_kind with no dedicated entry (greenhouse, lever, ashby, smartrecruiters, workable,
# rippling, bamboohr, recruitee, personio, jsonld, and any future/unknown kind) falls back to this:
# one form step the assist fills, then the human-gated review (research doc: "one step per page").
_GENERIC_PLAYBOOK: list[PlaybookStep] = [
    PlaybookStep(id="form", title="Fill application form", kind="form", user_only=False),
    PlaybookStep(id="review", title="Review", kind="review", user_only=True),
]

PLAYBOOKS: dict[str, list[PlaybookStep]] = {
    "workday": _WORKDAY_PLAYBOOK,
    _GENERIC_KIND: _GENERIC_PLAYBOOK,
}


def get_playbook(ats_kind: str) -> list[PlaybookStep]:
    """
    The apply playbook for one ATS kind.

    Args:
        ats_kind: One of pipeline.jobs.ats.ADAPTER_KINDS, e.g. "workday" or "greenhouse".

    Returns:
        PLAYBOOKS[ats_kind] when a dedicated playbook exists; otherwise the generic one-form-step-
        plus-review fallback. Never raises — an unknown or not-yet-onboarded ats_kind is exactly
        the case the fallback exists for.
    """
    return PLAYBOOKS.get(ats_kind, PLAYBOOKS[_GENERIC_KIND])


# The 8 canonical JobApplication.status values (pipeline/db.py JobApplication docstring, authoritative).
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "saved": {"preparing", "withdrawn"},
    "preparing": {"ready", "withdrawn"},
    "ready": {"submitted", "withdrawn"},
    "submitted": {"interviewing", "rejected", "withdrawn"},
    "interviewing": {"rejected", "closed", "withdrawn"},
    "rejected": set(),
    "closed": set(),
    "withdrawn": set(),
}


def transition(application: JobApplication, to_status: str) -> JobApplication:
    """
    Moves application to to_status, refusing any move _ALLOWED_TRANSITIONS doesn't list.

    `ready -> submitted` must only be reached by a caller acting on an explicit user action (e.g.
    the API route the user hits when they click "mark submitted"/finish an apply-assist session) —
    this function has no way to tell caller intent apart, so that distinction is enforced by the
    caller, never here. In particular the runner/scheduler must never call this with
    to_status="submitted" on its own.

    Args:
        application: The JobApplication row to mutate. Not committed here — the caller owns the
            session/commit, matching pipeline/domains/purchase.py's DomainPurchase status writes.
        to_status: One of the 8 canonical status values.

    Returns:
        The same application instance, with .status set to to_status.

    Raises:
        ValueError: to_status is not a legal move from application.status (including moving to/from
            an unrecognized status string).
    """
    current = application.status
    allowed = _ALLOWED_TRANSITIONS.get(current)
    if allowed is None or to_status not in allowed:
        raise ValueError(f"illegal application status transition: {current!r} -> {to_status!r}")
    application.status = to_status
    return application
