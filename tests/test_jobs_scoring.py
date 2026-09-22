"""
Tests for pipeline.jobs.scoring: prefilter() (free keyword/targeting checks) and score_new()
(prefilter + batched Claude JobFit scoring). Real (in-memory) SQLite session per
tests/test_jobs_inventory.py's pattern; the Anthropic client (a true external) is a scripted
FakeClient, following tests/test_jobs_research.py's style. Nothing under pipeline/ is mocked.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import JobBoard, JobCompany, JobPosting, JobsProfile  # noqa: E402
from pipeline.jobs import JobsSettings, scoring  # noqa: E402
from pipeline.triage import TriageConfigError  # noqa: E402


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic: each parse() pops the next response or raises it."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.messages = SimpleNamespace(parse=self._parse)
        self.calls: list[dict] = []

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=cast(Any, SimpleNamespace(method="POST", url="https://example.com/v1/messages"))
    )


def score_reply(items: list[tuple[str, int, str]], stop_reason: str = "end_turn") -> SimpleNamespace:
    parsed = scoring.JobFitBatch(
        items=[scoring.JobFit(alias=alias, score=score, reason=reason) for alias, score, reason in items]
    )
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _settings(**overrides):
    base = dict(
        refresh_cron="0 6 * * *",
        score_threshold=0.6,
        missed_polls_to_close=3,
        max_llm_resolves_per_run=10,
        max_score_per_run=50,
    )
    base.update(overrides)
    return JobsSettings(**base)


def _company(session, name="Acme Corp", domain="acme.com"):
    row = JobCompany(name=name, domain=domain, source="watchlist", status="active")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _board(session, company_id, ats_kind="greenhouse", board_id="acme"):
    row = JobBoard(company_id=company_id, ats_kind=ats_kind, board_id=board_id, resolved_by="pattern", confidence=1.0)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _posting(session, board_id, external_id="1", **overrides):
    fields = dict(
        board_id=board_id,
        external_id=external_id,
        title="Senior Backend Engineer",
        location="Remote - US",
        remote=True,
        comp_text="$180k-$220k",
        apply_url="https://acme.example/apply/1",
        content_hash="hash",
    )
    fields.update(overrides)
    row = JobPosting(**fields)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _profile(**overrides):
    fields = dict(
        target_roles="[]",
        target_locations="[]",
        target_remote=None,
        target_salary_floor=None,
        target_exclusions="[]",
    )
    fields.update(overrides)
    return JobsProfile(**fields)


# --------------------------------------------------------------------------------------------
# prefilter()
# --------------------------------------------------------------------------------------------


def test_prefilter_empty_profile_passes_everything(session):
    company = _company(session)
    posting = _posting(session, board_id=1, title="Anything At All", location="Nowhere", remote=False)
    assert scoring.prefilter(posting, company, _profile()) is None


def test_prefilter_rejects_excluded_company():
    company = JobCompany(name="Evil Corp", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Engineer", apply_url="https://x/1", content_hash="h"
    )
    profile = _profile(target_exclusions='["Evil Corp"]')
    assert scoring.prefilter(posting, company, profile) == "filtered: excluded company"


def test_prefilter_exclusion_is_case_insensitive_substring():
    company = JobCompany(name="Evil Corp Holdings", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Engineer", apply_url="https://x/1", content_hash="h"
    )
    profile = _profile(target_exclusions='["evil corp"]')
    assert scoring.prefilter(posting, company, profile) == "filtered: excluded company"


def test_prefilter_rejects_no_role_match():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Marketing Manager", apply_url="https://x/1", content_hash="h"
    )
    profile = _profile(target_roles='["Backend Engineer", "Platform Engineer"]')
    assert scoring.prefilter(posting, company, profile) == "filtered: no target role match"


def test_prefilter_accepts_loose_role_token_overlap():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Staff Backend Engineer", apply_url="https://x/1", content_hash="h"
    )
    profile = _profile(target_roles='["Backend Engineer"]')
    assert scoring.prefilter(posting, company, profile) is None


def test_prefilter_remote_posting_always_passes_location_check():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Engineer", location="Nowhere in particular",
        remote=True, apply_url="https://x/1", content_hash="h",
    )
    profile = _profile(target_locations='["Austin, TX"]')
    assert scoring.prefilter(posting, company, profile) is None


def test_prefilter_rejects_no_location_match():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Engineer", location="Tokyo",
        remote=False, apply_url="https://x/1", content_hash="h",
    )
    profile = _profile(target_locations='["Austin, TX"]')
    assert scoring.prefilter(posting, company, profile) == "filtered: no location/remote match"


def test_prefilter_accepts_matching_location():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Engineer", location="Austin, TX",
        remote=False, apply_url="https://x/1", content_hash="h",
    )
    profile = _profile(target_locations='["Austin, TX"]')
    assert scoring.prefilter(posting, company, profile) is None


def test_prefilter_target_remote_true_rejects_non_remote_posting():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Engineer", location="Austin, TX",
        remote=False, apply_url="https://x/1", content_hash="h",
    )
    profile = _profile(target_remote=True)
    assert scoring.prefilter(posting, company, profile) == "filtered: no location/remote match"


def test_prefilter_malformed_json_columns_treated_as_no_constraint():
    company = JobCompany(name="Acme", source="watchlist")
    posting = JobPosting(
        board_id=1, external_id="1", title="Anything", apply_url="https://x/1", content_hash="h"
    )
    profile = _profile(target_roles="not json", target_locations="{}", target_exclusions="null")
    assert scoring.prefilter(posting, company, profile) is None


# --------------------------------------------------------------------------------------------
# score_new()
# --------------------------------------------------------------------------------------------


def test_score_new_no_candidates_returns_zeroed_summary(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    summary = scoring.score_new(session, _profile(), client=FakeClient())
    assert summary == scoring.ScoringSummary()


def test_score_new_ignores_already_scored_and_closed_postings(session, monkeypatch):
    from pipeline.clock import utcnow

    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="scored", fit_score=80, fit_reason="already scored")
    _posting(session, board.id, external_id="closed", closed_at=utcnow())

    client = FakeClient()
    summary = scoring.score_new(session, _profile(), client=client)

    assert summary == scoring.ScoringSummary()
    assert client.calls == []


def test_score_new_filters_without_calling_model(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session, name="Evil Corp")
    board = _board(session, company.id)
    posting = _posting(session, board.id)
    profile = _profile(target_exclusions='["Evil Corp"]')

    client = FakeClient()
    summary = scoring.score_new(session, profile, client=client)

    assert summary.filtered == 1
    assert summary.scored == 0
    assert client.calls == []
    session.refresh(posting)
    assert posting.fit_score == 0
    assert posting.fit_reason == "filtered: excluded company"


def test_score_new_scores_passing_posting_via_model(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)

    client = FakeClient(score_reply([("p001", 87, "Strong match on role and comp")]))
    summary = scoring.score_new(session, _profile(), client=client)

    assert summary.scored == 1
    assert summary.filtered == 0
    assert summary.failed == 0
    session.refresh(posting)
    assert posting.fit_score == 87
    assert posting.fit_reason == "Strong match on role and comp"
    assert len(client.calls) == 1


def test_score_new_clamps_out_of_range_score(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)

    client = FakeClient(score_reply([("p001", 500, "way over")]))
    scoring.score_new(session, _profile(), client=client)

    session.refresh(posting)
    assert posting.fit_score == 100


def test_score_new_respects_max_score_per_run_cap(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", lambda: _settings(max_score_per_run=1))
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1")
    _posting(session, board.id, external_id="2")

    client = FakeClient(score_reply([("p001", 70, "ok")]))
    summary = scoring.score_new(session, _profile(), client=client)

    assert summary.scored == 1
    assert summary.skipped == 1
    assert len(client.calls) == 1


def test_score_new_unconfigured_client_skips_all(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)

    def raise_config_error():
        raise TriageConfigError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(scoring, "make_client", raise_config_error)

    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id)

    summary = scoring.score_new(session, _profile())

    assert summary.skipped == 1
    assert summary.scored == 0
    assert summary.failed == 0


def test_score_new_batch_failure_is_isolated_to_that_batch(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    monkeypatch.setattr(scoring, "BATCH_SIZE", 1)

    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1")
    _posting(session, board.id, external_id="2")

    client = FakeClient(connection_error(), score_reply([("p002", 60, "fine")]))
    summary = scoring.score_new(session, _profile(), client=client)

    assert summary.failed == 1
    assert summary.scored == 1
    assert len(client.calls) == 2


def test_score_new_refusal_counts_batch_as_failed(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)

    client = FakeClient(score_reply([], stop_reason="refusal"))
    summary = scoring.score_new(session, _profile(), client=client)

    assert summary.failed == 1
    assert summary.scored == 0
    session.refresh(posting)
    assert posting.fit_score is None


def test_score_new_model_omitting_a_posting_counts_it_failed(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1")
    _posting(session, board.id, external_id="2")

    client = FakeClient(score_reply([("p001", 50, "ok")]))  # p002 omitted
    summary = scoring.score_new(session, _profile(), client=client)

    assert summary.scored == 1
    assert summary.failed == 1


def test_score_new_duplicate_alias_keeps_first(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)

    client = FakeClient(score_reply([("p001", 40, "first"), ("p001", 90, "second")]))
    scoring.score_new(session, _profile(), client=client)

    session.refresh(posting)
    assert posting.fit_score == 40
    assert posting.fit_reason == "first"


def test_score_new_uses_make_client_when_no_client_passed(session, monkeypatch):
    monkeypatch.setattr(scoring, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    posting = _posting(session, board.id)

    fake = FakeClient(score_reply([("p001", 65, "default client path")]))
    monkeypatch.setattr(scoring, "make_client", lambda: fake)

    summary = scoring.score_new(session, _profile())

    assert summary.scored == 1
    session.refresh(posting)
    assert posting.fit_score == 65
