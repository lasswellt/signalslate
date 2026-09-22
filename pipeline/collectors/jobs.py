"""
Digest collector for the "jobs" source: turns pipeline.jobs job-tracking state (JobPosting,
JobApplication) into digest Items. No network, no credentials — like pipeline.collectors.domains,
this collector only reads what a separate scheduled job already wrote to the database
(pipeline.jobs.inventory.resolve_pending/poll_all and pipeline.jobs.scoring.score_new, run from
pipeline/scheduler.py's jobs_refresh job on JOBS_REFRESH_CRON), which is why check_jobs() in
pipeline.health can be a plain row count with no external dependency to fail.

Three kinds of Item per digest window ([since, until), matching every other source's convention):

1. job_new: a JobPosting first seen in the window whose fit_score has cleared
   jobs_settings().score_threshold. score_threshold is a 0-1 fraction (JOBS_SCORE_THRESHOLD,
   default 0.6) while fit_score is stored 0-100 (pipeline/db.py JobPosting docstring), so the
   threshold is scaled by 100 before comparing. An unscored posting (fit_score is still None,
   queued for a future scoring.score_new() run) never fires here, and neither does one scored
   below the bar.
2. job_closed: a JobPosting whose closed_at falls in the window, but only when it has an
   associated JobApplication — a closed posting nobody ever applied to or tracked isn't
   digest-worthy (the user never has to hear that a posting they ignored disappeared).
3. application_update: a JobApplication whose submitted_at falls in the window. The current
   schema (pipeline/db.py JobApplication, from T-016) has no "status last changed at" column, only
   submitted_at, so this is the one state transition this collector can report without a schema
   change (out of this task's scope — pipeline/db.py is not in SCOPE_FILES). Tracking every status
   transition (queued -> in_progress -> withdrawn/rejected/offer) would need a new timestamp
   column added to JobApplication in a later task.

external_id is stable across an unchanged event (job_new/application_update key off the row's own
id, which never gets reassigned) while staying distinct across genuinely different events on the
same posting: job_closed folds in the closed_at timestamp, so a posting that reopens (missed_polls
resets) then closes again later gets a new event id instead of silently reusing the old one —
matching domains.py's dedup-by-id discipline.

A broken DB session degrades the whole collection to CollectionResult(status="error") rather than
raising — mirroring check_domains()'s "only a broken DB session counts as unhealthy" framing, just
applied to collection instead of health-check. A single malformed row (an application pointing at a
posting that no longer exists) is skipped and counted in `failures` instead, matching domains.py's
per-row isolation: one bad row must not cost every other item in the run.
"""
import logging
from datetime import datetime

from sqlmodel import col, select

from pipeline import db
from pipeline.collectors import CollectionResult, Item
from pipeline.db import JobApplication, JobBoard, JobCompany, JobPosting
from pipeline.jobs import jobs_settings

_log = logging.getLogger(__name__)


def _company_by_board(session: "db.Session", board_ids: set[int]) -> dict[int, str]:
    """board_id -> company name, one query pair instead of one query per posting."""
    if not board_ids:
        return {}
    boards = session.exec(select(JobBoard).where(col(JobBoard.id).in_(board_ids))).all()
    company_ids = {board.company_id for board in boards}
    companies = {
        company.id: company.name
        for company in session.exec(select(JobCompany).where(col(JobCompany.id).in_(company_ids))).all()
    }
    return {board.id: companies.get(board.company_id, "") for board in boards if board.id is not None}


def collect_jobs(since: datetime, until: datetime) -> CollectionResult:
    """
    Reads JobPosting/JobApplication state for job_new, job_closed and application_update events in
    [since, until) — see module docstring for the three event kinds and their windowing.

    Args:
        since: Start of the collection window (inclusive), naive UTC.
        until: End of the collection window (exclusive), naive UTC.

    Returns:
        CollectionResult(source="jobs", ...). status is "error" only when the DB session itself
        fails (nothing could be read); "partial" when an individual row was skipped as malformed
        (e.g. an application referencing a since-deleted posting) while the rest of the run still
        completed; otherwise "ok".
    """
    items: list[Item] = []
    failures: list[str] = []

    try:
        with db.get_session() as session:
            threshold = jobs_settings().score_threshold * 100

            new_postings = session.exec(
                select(JobPosting).where(
                    col(JobPosting.first_seen) >= since,
                    col(JobPosting.first_seen) < until,
                )
            ).all()
            closed_postings = session.exec(
                select(JobPosting).where(
                    col(JobPosting.closed_at).is_not(None),
                    col(JobPosting.closed_at) >= since,
                    col(JobPosting.closed_at) < until,
                )
            ).all()
            submitted_applications = session.exec(
                select(JobApplication).where(
                    col(JobApplication.submitted_at).is_not(None),
                    col(JobApplication.submitted_at) >= since,
                    col(JobApplication.submitted_at) < until,
                )
            ).all()

            closed_ids = {posting.id for posting in closed_postings if posting.id is not None}
            applications_by_posting: dict[int, JobApplication] = {}
            if closed_ids:
                for application in session.exec(
                    select(JobApplication).where(col(JobApplication.posting_id).in_(closed_ids))
                ).all():
                    applications_by_posting[application.posting_id] = application

            postings_by_id: dict[int, JobPosting] = {
                posting.id: posting for posting in [*new_postings, *closed_postings] if posting.id is not None
            }
            missing_posting_ids = {
                application.posting_id for application in submitted_applications
            } - postings_by_id.keys()
            if missing_posting_ids:
                for posting in session.exec(
                    select(JobPosting).where(col(JobPosting.id).in_(missing_posting_ids))
                ).all():
                    if posting.id is not None:
                        postings_by_id[posting.id] = posting

            board_ids = {posting.board_id for posting in postings_by_id.values()}
            company_by_board = _company_by_board(session, board_ids)

            for posting in new_postings:
                if posting.id is None or posting.fit_score is None or posting.fit_score < threshold:
                    continue
                items.append(Item(
                    item_type="job_new",
                    external_id=f"job_new:{posting.id}",
                    occurred_at=posting.first_seen,
                    payload={
                        "posting_id": posting.id,
                        "title": posting.title,
                        "company": company_by_board.get(posting.board_id, ""),
                        "fit_score": posting.fit_score,
                        "fit_reason": posting.fit_reason,
                        "apply_url": posting.apply_url,
                    },
                ))

            for posting in closed_postings:
                if posting.id is None or posting.closed_at is None:
                    continue
                application = applications_by_posting.get(posting.id)
                if application is None:
                    continue  # nobody applied/tracked this posting — not digest-worthy
                items.append(Item(
                    item_type="job_closed",
                    external_id=f"job_closed:{posting.id}:{posting.closed_at.isoformat()}",
                    occurred_at=posting.closed_at,
                    payload={
                        "posting_id": posting.id,
                        "title": posting.title,
                        "company": company_by_board.get(posting.board_id, ""),
                        "application_status": application.status,
                    },
                ))

            for application in submitted_applications:
                if application.id is None or application.submitted_at is None:
                    continue
                posting = postings_by_id.get(application.posting_id)
                if posting is None:
                    _log.warning(
                        "jobs collector: application %s references missing posting %s",
                        application.id, application.posting_id,
                    )
                    failures.append(f"application {application.id}: posting {application.posting_id} missing")
                    continue
                items.append(Item(
                    item_type="application_update",
                    external_id=f"application_update:{application.id}:submitted",
                    occurred_at=application.submitted_at,
                    payload={
                        "posting_id": posting.id,
                        "title": posting.title,
                        "company": company_by_board.get(posting.board_id, ""),
                        "status": "submitted",
                    },
                ))
    except Exception as exc:  # noqa: BLE001 — a broken session must degrade, never crash the run
        _log.warning("jobs collector: session error: %s", type(exc).__name__)
        return CollectionResult("jobs", "error", type(exc).__name__)

    detail = f"{len(items)} item(s)"
    if failures:
        return CollectionResult("jobs", "partial", f"{detail} ({len(failures)} row(s) skipped: {failures[0]})", items)
    return CollectionResult("jobs", "ok", detail, items)
