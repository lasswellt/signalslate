"""
Inventory for pipeline/jobs/: resolve_pending() (JobCompany -> JobBoard, T-011/T-012 ladder) and
poll_all() (JobBoard -> JobPosting upsert/close), the two scheduled steps behind the jobs collector.

Design decisions (docs/_research/2026-09-21_jobs-collector.md §Dedup / change detection, §Risks):

- resolve_pending() only considers active JobCompany rows with no existing JobBoard row at all —
  a company already resolved is not re-attempted here (a board that later starts 404ing is
  poll_all()'s last_error concern, not a re-resolution case; out of scope for this task). It runs
  resolve_company() (free, no network budget concern) for every pending company, and only falls
  through to research_company() (paid LLM call) on a miss, capped at
  jobs_settings().max_llm_resolves_per_run calls total for the whole run via a local counter —
  once the cap is hit, a company whose resolve_fn also misses is simply left unresolved for a
  future run (no error, no row), matching the Risks table's "LLM cost creep... per-run cap,
  following triage batch sizing" mitigation.
- Mirrors pipeline/domains/inventory.py's per-item error isolation: one company's resolve_fn/
  research_fn raising an unexpected exception is caught, logged by exception CLASS only (never the
  message — same pipeline/redact.py reasoning: a resolver/LLM error can echo request/response
  detail) and counted as a failure, while every other company still runs.
- poll_all() processes every JobBoard row in a single, plain sequential loop — "serial per host"
  per the Risks table ("Rate limiting / IP blocks from aggressive polling... Daily cadence, serial
  per host") is satisfied by never polling two boards concurrently; no threading/async is needed
  at this volume (one run a day).
- A board's list_postings() raising (fetch_json's RuntimeError "unavailable" contract, per
  greenhouse.py/etc.) sets JobBoard.last_error and moves on: this board's JobPosting rows are left
  completely untouched this cycle, so a transient network/5xx flake never counts toward
  missed_polls — "a failing board never fails the run" (Risks table), and a transient flake must
  never look like evidence the postings are gone.
- JobPosting is a state row like Domain, never hard-deleted: an existing, not-yet-closed posting
  missing from this poll's returned set only gets missed_polls incremented, and only crosses over
  to closed_at once missed_polls reaches jobs_settings().missed_polls_to_close — "closed means
  missing from N consecutive polls (not 1, to absorb transient errors)" (research doc). A posting
  that reappears after being closed has closed_at cleared and is counted as reopened.
- Both functions take an open session from the caller (this module has no request/CLI entrypoint of
  its own yet, matching pipeline/jobs/seeds.py's convention) but, per the task's transaction
  discipline note, commit it themselves rather than leaving that to the caller: once per company in
  resolve_pending() and once per board in poll_all(), mirroring pipeline/domains/inventory.py's
  _sync_one() (one commit per connection, covering every row touched for that connection, not
  per-row) — a later company/board's failure can't discard an earlier one's already-flushed change.
- resolve_fn/research_fn/get_adapter_fn are injectable keyword args, matching sync_all's
  registrar_factory and refresh_snapshots' inspect_fn: tests exercise the real upsert/missed_polls
  logic against fakes rather than mocking pipeline.jobs.resolve/research/ats.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from sqlmodel import select

from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import JobBoard, JobCompany, JobPosting
from pipeline.jobs import content_hash, jobs_settings
from pipeline.jobs.ats import AtsAdapter, get_adapter
from pipeline.jobs.research import research_company
from pipeline.jobs.resolve import ResolveResult, resolve_company

_log = logging.getLogger(__name__)


@dataclass
class ResolveSummary:
    """resolve_pending()'s outcome: how many pending companies landed in each bucket."""
    resolved: int = 0  # a JobBoard row was inserted (resolve_fn or research_fn hit)
    skipped: int = 0  # every step missed, or the per-run LLM cap was already reached
    llm_used: int = 0  # research_fn was actually invoked (hit or miss)
    failed: int = 0  # resolve_fn/research_fn raised an unexpected exception


@dataclass
class PollSummary:
    """poll_all()'s outcome: how every board and its postings fared this run."""
    boards_polled: int = 0
    boards_failed: int = 0
    postings_upserted: int = 0
    postings_closed: int = 0
    postings_reopened: int = 0


def _pending_companies(session: "db.Session") -> list[JobCompany]:
    """Active JobCompany rows with no existing JobBoard row at all."""
    resolved_ids = set(session.exec(select(JobBoard.company_id)).all())
    companies = session.exec(select(JobCompany).where(JobCompany.status == "active")).all()
    return [company for company in companies if company.id not in resolved_ids]


def resolve_pending(
    session: "db.Session",
    *,
    resolve_fn: Callable[[JobCompany], Optional[ResolveResult]] = resolve_company,
    research_fn: Callable[[str, Optional[str]], tuple[Optional[ResolveResult], str]] = research_company,
) -> ResolveSummary:
    """
    Resolves a JobBoard for every active, not-yet-resolved JobCompany.

    Args:
        session: An open SQLModel Session; committed once per company processed.
        resolve_fn: pipeline.jobs.resolve.resolve_company-shaped callable (the free, deterministic
            ladder), tried first for every pending company. Tests inject a fake.
        research_fn: pipeline.jobs.research.research_company-shaped callable (the paid LLM step),
            tried only on a resolve_fn miss and only while under this run's LLM-resolve cap
            (jobs_settings().max_llm_resolves_per_run). Tests inject a fake.

    Returns:
        ResolveSummary counting how many pending companies were resolved, skipped (no hit, or the
        LLM cap was already reached), had research_fn actually invoked, or failed with an
        unexpected exception.
    """
    settings = jobs_settings()
    summary = ResolveSummary()
    llm_calls = 0

    for company in _pending_companies(session):
        try:
            result = resolve_fn(company)
            if result is None:
                if llm_calls >= settings.max_llm_resolves_per_run:
                    summary.skipped += 1
                    continue
                llm_calls += 1
                summary.llm_used += 1
                result, _reason = research_fn(company.name, company.domain)
        except Exception as exc:  # one company's resolver failure must never stop the others
            _log.warning("jobs resolve_pending: company %s failed: %s", company.id, type(exc).__name__)
            summary.failed += 1
            continue

        if result is None:
            summary.skipped += 1
            continue

        assert company.id is not None  # company came from a select() on the DB, so it always has a primary key
        session.add(
            JobBoard(
                company_id=company.id,
                ats_kind=result.ats_kind,
                board_id=result.board_id,
                resolved_by=result.resolved_by,
                confidence=result.confidence,
                verified_at=utcnow(),
            )
        )
        session.commit()
        summary.resolved += 1

    return summary


def _upsert_postings(
    session: "db.Session", board: JobBoard, raw_postings: list, now,
) -> tuple[int, int, int]:
    """One board's postings upsert + missed-polls/close pass. Returns
    (postings_upserted, postings_closed, postings_reopened) for this board."""
    settings = jobs_settings()
    upserted = closed = reopened = 0

    assert board.id is not None  # board came from a select() on the DB, so it always has a primary key
    existing = session.exec(select(JobPosting).where(JobPosting.board_id == board.id)).all()
    existing_by_external_id = {posting.external_id: posting for posting in existing}
    seen_external_ids = {raw.external_id for raw in raw_postings}

    for raw in raw_postings:
        new_hash = content_hash(
            raw.title, raw.description,
            location=raw.location, remote=raw.remote, comp_text=raw.comp_text, apply_url=raw.url,
        )
        posting = existing_by_external_id.get(raw.external_id)
        if posting is None:
            posting = JobPosting(
                board_id=board.id,
                external_id=raw.external_id,
                title=raw.title,
                location=raw.location,
                remote=raw.remote,
                comp_text=raw.comp_text,
                apply_url=raw.url,
                content_hash=new_hash,
                first_seen=now,
                last_seen=now,
                missed_polls=0,
            )
        else:
            posting.title = raw.title
            posting.location = raw.location
            posting.remote = raw.remote
            posting.comp_text = raw.comp_text
            posting.apply_url = raw.url
            posting.content_hash = new_hash
            posting.last_seen = now
            posting.missed_polls = 0
            if posting.closed_at is not None:
                posting.closed_at = None
                reopened += 1
        session.add(posting)
        upserted += 1

    for external_id, posting in existing_by_external_id.items():
        if external_id in seen_external_ids or posting.closed_at is not None:
            continue
        posting.missed_polls += 1
        if posting.missed_polls >= settings.missed_polls_to_close:
            posting.closed_at = now
            closed += 1
        session.add(posting)

    return upserted, closed, reopened


def poll_all(
    session: "db.Session", *, get_adapter_fn: Callable[[str], AtsAdapter] = get_adapter
) -> PollSummary:
    """
    Polls every resolved JobBoard for its current postings, upserting JobPosting rows.

    Args:
        session: An open SQLModel Session; committed once per board processed.
        get_adapter_fn: pipeline.jobs.ats.get_adapter-shaped callable. Tests inject a fake adapter
            factory instead of a real ATS/network call.

    Returns:
        PollSummary counting boards polled/failed and postings upserted/closed/reopened across
        every board. A board whose adapter raises (an "unavailable" transient flake, per each
        adapter's own contract) is recorded via JobBoard.last_error and skipped for this cycle —
        its JobPosting rows are left completely untouched, never counted toward missed_polls.
    """
    summary = PollSummary()

    for board in session.exec(select(JobBoard)).all():
        try:
            adapter = get_adapter_fn(board.ats_kind)
            raw_postings = adapter.list_postings(board.board_id)
        except Exception as exc:  # one board's adapter failure must never stop the others
            _log.warning("jobs poll_all: board %s failed: %s", board.id, type(exc).__name__)
            board.last_error = type(exc).__name__
            session.add(board)
            session.commit()
            summary.boards_failed += 1
            continue

        now = utcnow()
        upserted, closed, reopened = _upsert_postings(session, board, raw_postings, now)

        board.last_error = None
        board.last_polled_at = now
        session.add(board)
        session.commit()

        summary.boards_polled += 1
        summary.postings_upserted += upserted
        summary.postings_closed += closed
        summary.postings_reopened += reopened

    return summary
