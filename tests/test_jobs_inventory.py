"""
Tests for pipeline.jobs.inventory: resolve_pending() (JobCompany -> JobBoard) and poll_all()
(JobBoard -> JobPosting upsert/close). Real (in-memory) SQLite session per tests/test_job_tables.py's
pattern. resolve_fn/research_fn/get_adapter_fn are injected fakes — no real network, no real
pipeline.jobs.resolve/research/ats calls.
"""
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import JobBoard, JobCompany, JobPosting  # noqa: E402
from pipeline.jobs import JobsSettings, inventory  # noqa: E402
from pipeline.jobs.ats import RawPosting  # noqa: E402
from pipeline.jobs.resolve import ResolveResult  # noqa: E402


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


def _company(session, name="Acme Corp", domain="acme.com", status="active"):
    row = JobCompany(name=name, domain=domain, source="watchlist", status=status)
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
        title="Engineer",
        apply_url="https://acme.example/apply/1",
        content_hash="original-hash",
        missed_polls=0,
    )
    fields.update(overrides)
    row = JobPosting(**fields)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# --------------------------------------------------------------------------------------------
# resolve_pending()
# --------------------------------------------------------------------------------------------


def test_resolve_pending_inserts_board_on_resolve_fn_hit(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)

    def resolve_fn(company):
        return ResolveResult(ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0)

    def research_fn(name, domain):
        raise AssertionError("research_fn must not be called when resolve_fn hits")

    summary = inventory.resolve_pending(session, resolve_fn=resolve_fn, research_fn=research_fn)

    assert (summary.resolved, summary.skipped, summary.llm_used, summary.failed) == (1, 0, 0, 0)
    board = session.exec(select(JobBoard).where(JobBoard.company_id == company.id)).one()
    assert (board.ats_kind, board.board_id, board.resolved_by, board.confidence) == ("greenhouse", "acme", "pattern", 1.0)
    assert board.verified_at is not None


def test_resolve_pending_falls_through_to_research_fn_on_miss(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    _company(session)
    calls = []

    def resolve_fn(company):
        return None

    def research_fn(name, domain):
        calls.append((name, domain))
        return ResolveResult(ats_kind="lever", board_id="acme", resolved_by="llm", confidence=0.8), ""

    summary = inventory.resolve_pending(session, resolve_fn=resolve_fn, research_fn=research_fn)

    assert (summary.resolved, summary.skipped, summary.llm_used, summary.failed) == (1, 0, 1, 0)
    assert calls == [("Acme Corp", "acme.com")]
    board = session.exec(select(JobBoard)).one()
    assert (board.ats_kind, board.resolved_by) == ("lever", "llm")


def test_resolve_pending_both_miss_leaves_company_unresolved(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    _company(session)

    summary = inventory.resolve_pending(
        session, resolve_fn=lambda c: None, research_fn=lambda name, domain: (None, "")
    )

    assert (summary.resolved, summary.skipped, summary.llm_used, summary.failed) == (0, 1, 1, 0)
    assert session.exec(select(JobBoard)).all() == []


def test_resolve_pending_respects_llm_cap(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", lambda: _settings(max_llm_resolves_per_run=1))
    _company(session, name="Acme Corp", domain="acme.com")
    _company(session, name="Beta Inc", domain="beta.com")
    research_calls = []

    def research_fn(name, domain):
        research_calls.append(name)
        return None, ""

    summary = inventory.resolve_pending(session, resolve_fn=lambda c: None, research_fn=research_fn)

    assert summary.llm_used == 1
    assert len(research_calls) == 1
    assert summary.skipped == 2  # one research miss, one cap-skip
    assert session.exec(select(JobBoard)).all() == []


def test_resolve_pending_skips_already_resolved_companies(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    _board(session, company.id)

    def resolve_fn(company):
        raise AssertionError("resolve_fn must not be called for an already-resolved company")

    summary = inventory.resolve_pending(session, resolve_fn=resolve_fn, research_fn=lambda n, d: (None, ""))
    assert (summary.resolved, summary.skipped, summary.llm_used, summary.failed) == (0, 0, 0, 0)


def test_resolve_pending_skips_muted_companies(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    _company(session, status="muted")

    def resolve_fn(company):
        raise AssertionError("resolve_fn must not be called for a muted company")

    summary = inventory.resolve_pending(session, resolve_fn=resolve_fn, research_fn=lambda n, d: (None, ""))
    assert (summary.resolved, summary.skipped, summary.llm_used, summary.failed) == (0, 0, 0, 0)


def test_resolve_pending_isolates_resolve_fn_exception(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    _company(session, name="Acme Corp", domain="acme.com")
    good = _company(session, name="Beta Inc", domain="beta.com")

    def resolve_fn(company):
        if company.name == "Acme Corp":
            raise RuntimeError("boom")
        return ResolveResult(ats_kind="greenhouse", board_id="beta", resolved_by="pattern", confidence=1.0)

    summary = inventory.resolve_pending(session, resolve_fn=resolve_fn, research_fn=lambda n, d: (None, ""))

    assert (summary.resolved, summary.failed) == (1, 1)
    board = session.exec(select(JobBoard)).one()
    assert board.company_id == good.id


def test_resolve_pending_isolates_research_fn_exception(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    _company(session)

    def research_fn(name, domain):
        raise ValueError("boom")

    summary = inventory.resolve_pending(session, resolve_fn=lambda c: None, research_fn=research_fn)
    assert (summary.resolved, summary.failed, summary.llm_used) == (0, 1, 1)
    assert session.exec(select(JobBoard)).all() == []


# --------------------------------------------------------------------------------------------
# poll_all()
# --------------------------------------------------------------------------------------------


class FakeAdapter:
    def __init__(self, postings=None, raises=None):
        self.postings = postings or []
        self.raises = raises
        self.calls = []

    def list_postings(self, board_id):
        self.calls.append(board_id)
        if self.raises:
            raise self.raises
        return self.postings


def test_poll_all_inserts_new_posting(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    raw = RawPosting(external_id="1", title="Engineer", url="https://acme.example/apply/1", description="desc")
    adapter = FakeAdapter(postings=[raw])

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    assert (summary.boards_polled, summary.boards_failed, summary.postings_upserted) == (1, 0, 1)
    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert (posting.external_id, posting.title, posting.missed_polls, posting.closed_at) == ("1", "Engineer", 0, None)
    assert posting.first_seen == posting.last_seen
    refreshed_board = session.get(JobBoard, board.id)
    assert refreshed_board.last_error is None
    assert refreshed_board.last_polled_at is not None


def test_poll_all_updates_existing_posting_and_resets_missed_polls(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1", title="Old Title", missed_polls=2)
    raw = RawPosting(external_id="1", title="New Title", url="https://acme.example/apply/1", description="desc")
    adapter = FakeAdapter(postings=[raw])

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    assert summary.postings_upserted == 1
    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert posting.title == "New Title"
    assert posting.missed_polls == 0
    assert posting.content_hash != "original-hash"


def test_poll_all_increments_missed_polls_when_absent(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1", missed_polls=0)
    adapter = FakeAdapter(postings=[])

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert posting.missed_polls == 1
    assert posting.closed_at is None
    assert summary.postings_closed == 0


def test_poll_all_closes_posting_after_threshold(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", lambda: _settings(missed_polls_to_close=2))
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1", missed_polls=1)
    adapter = FakeAdapter(postings=[])

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert posting.missed_polls == 2
    assert posting.closed_at is not None
    assert summary.postings_closed == 1


def test_poll_all_reopens_closed_posting_that_reappears(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1", missed_polls=3, closed_at=utcnow())
    raw = RawPosting(external_id="1", title="Engineer", url="https://acme.example/apply/1", description="desc")
    adapter = FakeAdapter(postings=[raw])

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert posting.closed_at is None
    assert posting.missed_polls == 0
    assert summary.postings_reopened == 1


def test_poll_all_does_not_reincrement_already_closed_posting(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1", missed_polls=3, closed_at=utcnow())
    adapter = FakeAdapter(postings=[])

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert posting.missed_polls == 3
    assert summary.postings_closed == 0


def test_poll_all_records_error_and_leaves_postings_untouched_on_failure(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    company = _company(session)
    board = _board(session, company.id)
    _posting(session, board.id, external_id="1", missed_polls=0)
    adapter = FakeAdapter(raises=RuntimeError("unavailable"))

    summary = inventory.poll_all(session, get_adapter_fn=lambda kind: adapter)

    assert (summary.boards_polled, summary.boards_failed) == (0, 1)
    refreshed_board = session.get(JobBoard, board.id)
    assert refreshed_board.last_error == "RuntimeError"
    posting = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).one()
    assert posting.missed_polls == 0
    assert posting.closed_at is None


def test_poll_all_isolates_one_board_failure_from_another(session, monkeypatch):
    monkeypatch.setattr(inventory, "jobs_settings", _settings)
    bad_company = _company(session, name="Bad Co", domain="bad.com")
    bad_board = _board(session, bad_company.id, ats_kind="greenhouse", board_id="bad")
    good_company = _company(session, name="Good Co", domain="good.com")
    good_board = _board(session, good_company.id, ats_kind="lever", board_id="good")

    bad_adapter = FakeAdapter(raises=RuntimeError("boom"))
    good_raw = RawPosting(external_id="1", title="Engineer", url="https://good.example/apply/1")
    good_adapter = FakeAdapter(postings=[good_raw])

    def get_adapter_fn(kind):
        return bad_adapter if kind == "greenhouse" else good_adapter

    summary = inventory.poll_all(session, get_adapter_fn=get_adapter_fn)

    assert (summary.boards_polled, summary.boards_failed) == (1, 1)
    assert session.get(JobBoard, bad_board.id).last_error == "RuntimeError"
    assert session.get(JobBoard, good_board.id).last_error is None
    posting = session.exec(select(JobPosting).where(JobPosting.board_id == good_board.id)).one()
    assert posting.external_id == "1"
