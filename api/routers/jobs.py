"""
Jobs collector routes (docs/_research/2026-09-21_jobs-collector.md), mirroring api.routers.domains.

Design decisions:

- Reads (companies/postings list) query pipeline.db.JobCompany/JobBoard/JobPosting directly with
  sqlmodel select, the same pattern api.routers.domains uses for Domain/DomainSnapshot: none of
  pipeline.jobs.seeds/inventory expose a list function of their own (they're all mutations), so
  there's nothing to call through for a plain filtered read.
- GET /jobs/postings joins JobPosting -> JobBoard -> JobCompany the same way
  pipeline.collectors.jobs._company_by_board does: two follow-up id->row queries and Python dict
  lookups, not a SQL JOIN — no SQL join appears anywhere else in this codebase, so this stays
  consistent with it rather than introducing a new query style for one endpoint.
- Every pipeline.jobs mutation module is imported by module (seeds, resolve, research, ats,
  inventory), never by name, so tests can monkeypatch it at this file's own import site
  (jobs_router.seeds.add_company, jobs_router.resolve.resolve_company, ...) instead of touching
  pipeline.jobs.* or the network/LLM directly.
- POST /jobs/companies/{id}/rescan runs the same per-company resolve step
  (pipeline.jobs.resolve.resolve_company, falling through to pipeline.jobs.research.
  research_company on a miss) and per-board poll step (pipeline.jobs.ats.get_adapter +
  pipeline.jobs.inventory._upsert_postings) that resolve_pending()/poll_all() run for every
  pending/resolved row, scoped to just this one company/its boards so a rescan reflects instantly
  rather than waiting for the next scheduled jobs_refresh run. research_company (the paid LLM
  step, with web_search/web_fetch tool calls) has no caller-side timeout of its own, so it's run in
  a worker thread with a wall-clock timeout here: one slow/hung research call must never hang this
  request forever. A timeout degrades to "no hit", the same shape research_company itself already
  returns on a call failure — it never surfaces as a 5xx.
- Errors are {code, message} (api.routers.domains._coded pattern), never raw exception text.
- PUT /jobs/boards/{id} is the manual override: it always sets resolved_by="manual",
  confidence=1.0 and a fresh verified_at, on the theory that a human confirming a board by hand is
  the most trustworthy signal available.
"""
import concurrent.futures
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from api.serialize import iso_z
from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import JobBoard, JobCompany, JobPosting
from pipeline.jobs import ats, inventory, research, resolve, seeds

router = APIRouter(tags=["jobs"])

_log = logging.getLogger(__name__)

# A pasted CSV import body is capped well above any real watchlist (one line per company) but far
# short of a request that could tie up the worker thread parsing it (api.routers.domains.
# ImportBody's own _MAX_IMPORT_CHARS reasoning).
_MAX_IMPORT_CHARS = 200_000

# Wall-clock cap on one rescan's research_company() call: web_search/web_fetch tool round-trips can
# be slow, and this endpoint must return in a bounded time even when the LLM step hangs or is slow.
_RESCAN_RESEARCH_TIMEOUT_SECONDS = 25

_rescan_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="jobs-rescan")


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


class BoardOut(BaseModel):
    id: int
    ats_kind: str
    board_id: str
    resolved_by: str
    confidence: float
    verified_at: Optional[str]
    last_polled_at: Optional[str]
    last_error: Optional[str]


class CompanyOut(BaseModel):
    id: int
    name: str
    domain: Optional[str]
    source: str
    status: str
    first_seen: str
    last_seen: str
    boards: list[BoardOut]


class AddCompanyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    domain: Optional[str] = None


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(max_length=_MAX_IMPORT_CHARS)


class RejectedRow(BaseModel):
    line: int
    reason: str


class ImportOut(BaseModel):
    added: list[str]
    rejected: list[RejectedRow]


class BoardOverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ats_kind: str
    board_id: str


class PostingOut(BaseModel):
    id: int
    title: str
    location: Optional[str]
    remote: Optional[bool]
    comp_text: Optional[str]
    apply_url: str
    first_seen: str
    last_seen: str
    closed_at: Optional[str]
    fit_score: Optional[int]
    fit_reason: Optional[str]
    company_name: str
    ats_kind: str


def _board_out(row: JobBoard) -> BoardOut:
    assert row.id is not None  # row came from a select() on the DB, so it always has a primary key
    return BoardOut(
        id=row.id,
        ats_kind=row.ats_kind,
        board_id=row.board_id,
        resolved_by=row.resolved_by,
        confidence=row.confidence,
        verified_at=iso_z(row.verified_at),
        last_polled_at=iso_z(row.last_polled_at),
        last_error=row.last_error,
    )


def _company_out(row: JobCompany, session: Any) -> CompanyOut:
    assert row.id is not None  # row came from a select() on the DB, so it always has a primary key
    boards = session.exec(select(JobBoard).where(JobBoard.company_id == row.id)).all()
    return CompanyOut(
        id=row.id,
        name=row.name,
        domain=row.domain,
        source=row.source,
        status=row.status,
        first_seen=iso_z(row.first_seen),
        last_seen=iso_z(row.last_seen),
        boards=[_board_out(b) for b in boards],
    )


def _posting_out(row: JobPosting, company_name: str, ats_kind: str) -> PostingOut:
    assert row.id is not None  # row came from a select() on the DB, so it always has a primary key
    return PostingOut(
        id=row.id,
        title=row.title,
        location=row.location,
        remote=row.remote,
        comp_text=row.comp_text,
        apply_url=row.apply_url,
        first_seen=iso_z(row.first_seen),
        last_seen=iso_z(row.last_seen),
        closed_at=iso_z(row.closed_at),
        fit_score=row.fit_score,
        fit_reason=row.fit_reason,
        company_name=company_name,
        ats_kind=ats_kind,
    )


def _research_with_timeout(name: str, domain: Optional[str]):
    """Runs research.research_company() under _RESCAN_RESEARCH_TIMEOUT_SECONDS. A timeout is
    reported the same way research_company() itself reports any other call failure: (None,
    reason)."""
    future = _rescan_executor.submit(research.research_company, name, domain)
    try:
        return future.result(timeout=_RESCAN_RESEARCH_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        return None, "timeout"


@router.get("/jobs/companies", response_model=list[CompanyOut])
def list_companies(status: Optional[Literal["active", "muted"]] = None) -> list[CompanyOut]:
    """Every watchlist company, optionally filtered by status (active|muted)."""
    with db.get_session() as session:
        statement = select(JobCompany)
        if status is not None:
            statement = statement.where(JobCompany.status == status)
        rows = session.exec(statement.order_by(col(JobCompany.name))).all()
        return [_company_out(row, session) for row in rows]


@router.post("/jobs/companies", status_code=201, response_model=CompanyOut)
def add_company(body: AddCompanyBody) -> CompanyOut:
    """Manually adds/updates a watchlist company (source="manual")."""
    with db.get_session() as session:
        try:
            row = seeds.add_company(session, body.name, body.domain, source="manual")
        except ValueError as exc:
            raise _coded(422, "invalid_company", str(exc)) from None
        session.commit()
        session.refresh(row)
        return _company_out(row, session)


@router.post("/jobs/companies/import", response_model=ImportOut)
def import_companies(body: ImportBody) -> ImportOut:
    """Bulk add_company(source="watchlist") from CSV/plain text, one company (or "name,domain")
    per line."""
    with db.get_session() as session:
        result = seeds.import_csv(session, body.csv)
        session.commit()
        return ImportOut(
            added=result.added,
            rejected=[RejectedRow(line=line_number, reason=reason) for line_number, reason in result.rejected],
        )


@router.post("/jobs/companies/import-yc", response_model=ImportOut)
def import_companies_yc() -> ImportOut:
    """Imports companies from yc-oss's "hiring now" list (source="yc"). Opt-in, best-effort: an
    unreachable/malformed list degrades to an empty ImportOut rather than a 5xx."""
    with db.get_session() as session:
        result = seeds.import_yc(session)
        session.commit()
        return ImportOut(
            added=result.added,
            rejected=[RejectedRow(line=line_number, reason=reason) for line_number, reason in result.rejected],
        )


@router.post("/jobs/companies/import-hn", response_model=ImportOut)
def import_companies_hn() -> ImportOut:
    """Imports companies from HN's latest "Who is hiring" thread (source="hn"). Opt-in,
    best-effort: an unreachable/malformed thread degrades to an empty ImportOut rather than a
    5xx."""
    with db.get_session() as session:
        result = seeds.import_hn(session)
        session.commit()
        return ImportOut(
            added=result.added,
            rejected=[RejectedRow(line=line_number, reason=reason) for line_number, reason in result.rejected],
        )


@router.post("/jobs/companies/{id}/rescan", response_model=CompanyOut)
def rescan_company(id: int) -> CompanyOut:
    """
    Resolves (when this company has no JobBoard yet) and polls this one company's board(s)
    synchronously, then returns its current resolution state. See module docstring for why this
    mirrors resolve_pending()/poll_all() scoped to one company, and how the LLM research step is
    time-bounded.
    """
    with db.get_session() as session:
        company = session.get(JobCompany, id)
        if company is None:
            raise _coded(404, "company_not_found", "Company not found")

        boards = session.exec(select(JobBoard).where(JobBoard.company_id == id)).all()
        if not boards:
            result = None
            try:
                result = resolve.resolve_company(company)
                if result is None:
                    result, _reason = _research_with_timeout(company.name, company.domain)
            except Exception as exc:  # one company's resolver failure must never 500 the request
                _log.warning("jobs rescan: resolve failed for company %s: %s", id, type(exc).__name__)
                result = None
            if result is not None:
                board = JobBoard(
                    company_id=id,
                    ats_kind=result.ats_kind,
                    board_id=result.board_id,
                    resolved_by=result.resolved_by,
                    confidence=result.confidence,
                    verified_at=utcnow(),
                )
                session.add(board)
                session.commit()
                session.refresh(board)
                boards = [board]

        now = utcnow()
        for board in boards:
            try:
                adapter = ats.get_adapter(board.ats_kind)
                raw_postings = adapter.list_postings(board.board_id)
            except Exception as exc:  # one board's adapter failure must never 500 the request
                _log.warning("jobs rescan: poll failed for board %s: %s", board.id, type(exc).__name__)
                board.last_error = type(exc).__name__
                session.add(board)
                session.commit()
                continue
            inventory._upsert_postings(session, board, raw_postings, now)
            board.last_error = None
            board.last_polled_at = now
            session.add(board)
            session.commit()

        session.refresh(company)
        return _company_out(company, session)


@router.put("/jobs/boards/{id}", response_model=BoardOut)
def override_board(id: int, body: BoardOverrideBody) -> BoardOut:
    """Manual override of a resolved board's ats_kind/board_id, always stamped
    resolved_by="manual", confidence=1.0 and a fresh verified_at."""
    with db.get_session() as session:
        row = session.get(JobBoard, id)
        if row is None:
            raise _coded(404, "board_not_found", "Board not found")
        row.ats_kind = body.ats_kind
        row.board_id = body.board_id
        row.resolved_by = "manual"
        row.confidence = 1.0
        row.verified_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _board_out(row)


@router.get("/jobs/postings", response_model=list[PostingOut])
def list_postings(
    min_score: Optional[int] = None,
    status: Optional[Literal["open", "closed"]] = None,
    company: Optional[str] = None,
    text: Optional[str] = None,
    remote: Optional[bool] = None,
) -> list[PostingOut]:
    """Filterable posting list: min_score (fit_score >=), status (open = closed_at is None), company
    (substring match against the joined company name), text (substring match against title), remote."""
    with db.get_session() as session:
        statement = select(JobPosting)
        if min_score is not None:
            statement = statement.where(col(JobPosting.fit_score) >= min_score)
        if status == "open":
            statement = statement.where(col(JobPosting.closed_at).is_(None))
        elif status == "closed":
            statement = statement.where(col(JobPosting.closed_at).is_not(None))
        if remote is not None:
            statement = statement.where(JobPosting.remote == remote)
        if text:
            statement = statement.where(col(JobPosting.title).contains(text))

        if company:
            matching_company_ids = {
                row.id
                for row in session.exec(select(JobCompany).where(col(JobCompany.name).contains(company))).all()
                if row.id is not None
            }
            matching_board_ids = {
                row.id
                for row in session.exec(select(JobBoard).where(col(JobBoard.company_id).in_(matching_company_ids))).all()
                if row.id is not None
            } if matching_company_ids else set()
            if not matching_board_ids:
                return []
            statement = statement.where(col(JobPosting.board_id).in_(matching_board_ids))

        postings = session.exec(statement.order_by(col(JobPosting.last_seen).desc())).all()
        if not postings:
            return []

        board_ids = {posting.board_id for posting in postings}
        boards = {row.id: row for row in session.exec(select(JobBoard).where(col(JobBoard.id).in_(board_ids))).all()}
        company_ids = {board.company_id for board in boards.values()}
        companies = {
            row.id: row.name
            for row in (session.exec(select(JobCompany).where(col(JobCompany.id).in_(company_ids))).all() if company_ids else [])
        }

        out: list[PostingOut] = []
        for posting in postings:
            board = boards.get(posting.board_id)
            ats_kind = board.ats_kind if board is not None else ""
            company_name = companies.get(board.company_id, "") if board is not None else ""
            out.append(_posting_out(posting, company_name, ats_kind))
        return out
