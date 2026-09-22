"""
SQLite-backed run history and source health, via SQLModel. One file DB — fine at this volume
(one run a day, six source-health rows per run).
"""
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import UniqueConstraint, event
from sqlmodel import Field, Session, SQLModel, col, create_engine, func, select

from pipeline.clock import utcnow

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "digest.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """
    WAL lets the scheduler thread read while a manual run writes; without it the two can collide
    with "database is locked" once collection takes minutes instead of milliseconds. busy_timeout
    makes a writer wait for the lock rather than failing instantly.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    trigger: str  # "manual" | "scheduled"
    status: str = "running"  # running | success | partial | failed
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None  # short human-readable outcome
    error: Optional[str] = None
    pdf_path: Optional[str] = None  # populated once the render phase exists


class SourceHealth(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id")
    source: str  # "m365_<alias>" | "zoom" | "slack_<label>" | "gmail_<label>"
    status: str  # "ok" | "error"
    detail: Optional[str] = None
    checked_at: datetime = Field(default_factory=utcnow)


class CollectedItem(SQLModel, table=True):
    """
    One item pulled from one source during one run: a mail, an event, a task, a chat/Slack message,
    a Zoom meeting. Stored raw as JSON — normalization is Phase 3's job, and keeping the payload
    verbatim means a synthesis change doesn't require re-collecting.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    source: str = Field(index=True)  # "m365_<alias>" | "zoom" | "slack_<label>" | "gmail_<label>"
    item_type: str  # mail | event | task | chat | message | meeting
    external_id: str  # the source's own id — dedupes overlapping collection windows
    occurred_at: datetime  # the item's own timestamp, naive UTC
    payload: str  # JSON blob as returned by the source


class SourceCursor(SQLModel, table=True):
    """
    Per-source watermark, deliberately NOT keyed to a run: a failed run must not lose the last
    known-good position. Lives here rather than in config.json because config.json is user-editable
    web-UI state and a PUT /config full-replace would clobber it.
    """
    source: str = Field(primary_key=True)
    last_success_at: Optional[datetime] = None
    cursor: Optional[str] = None  # unused on filtered reads; present for a later delta/cursor source
    # Runs since this source last collected cleanly. A permanent sub-resource failure (To Do not
    # licensed, say) would otherwise freeze the watermark forever, re-fetching the full backfill
    # window every day for good. See runner.MAX_STUCK_RUNS.
    consecutive_failures: int = 0
    # Last collection attempt, successful or not. last_success_at only moves on a clean run, so it
    # cannot tell "never tried" from "tried and failed". These four columns are all nullable on
    # purpose: _add_missing_columns() silently skips a NOT NULL column without a Python default,
    # which would leave an existing install raising "no such column" on every cursor read.
    last_attempt_at: Optional[datetime] = None
    last_status: Optional[str] = None  # "ok" | "error"
    last_detail: Optional[str] = None
    last_item_count: Optional[int] = None


class Connection(SQLModel, table=True):
    """
    One configured source, editable from the management UI. `id` equals the source id used by
    CollectedItem.source, SourceHealth.source and SourceCursor.source, so nothing downstream
    needs a lookup.

    `config` holds NON-secret fields as JSON. `secret_ciphertext` is an encrypted JSON envelope of
    the secrets dict, kept in a plain nullable TEXT column and encrypted/decrypted only in the
    service module (a TypeDecorator would hide the crypto from callers and make every ORM load
    attempt a decrypt).
    """
    id: str = Field(primary_key=True)
    kind: str  # "m365" | "zoom" | "slack" | "gmail"
    label: str  # the alias for m365, the literal "zoom" for zoom
    origin: str  # "env" | "ui"
    seq: int = 0  # stable creation order; numbers the M365_ORG<n> env keys
    config: str  # JSON of non-secret fields
    secret_ciphertext: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Tombstone(SQLModel, table=True):
    """A deleted connection id: stops the startup .env seeding from re-creating a connection the user removed."""
    id: str = Field(primary_key=True)
    deleted_at: datetime = Field(default_factory=utcnow)


class Domain(SQLModel, table=True):
    """
    A domain in the owner's portfolio — state, not an event. Unlike CollectedItem, one row per
    domain is updated in place by each refresh rather than appended; `first_seen`/`last_seen`/
    `missing_since` track that lifecycle without a delete (a registrar drop should read as history,
    not disappear).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)  # lower-case punycode
    ownership: str = "owned"  # owned | watched
    source: str  # namecheap | godaddy | wordpress | manual
    connection_id: Optional[str] = Field(default=None, foreign_key="connection.id")
    expires_at: Optional[datetime] = None
    auto_renew: Optional[bool] = None
    locked: Optional[bool] = None
    privacy: Optional[bool] = None
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    # Set when a refresh no longer lists this domain at its registrar; cleared if it reappears.
    # Nullable/no-default columns above stay off an existing install's ALTER TABLE path (db.py:83);
    # this table is new, so create_all() covers it regardless — kept nullable for future columns.
    missing_since: Optional[datetime] = None


class DomainSnapshot(SQLModel, table=True):
    """
    One point-in-time capture of a domain's public state (DNS, RDAP, mail posture) as JSON. The
    `domains` digest collector diffs consecutive snapshots for one domain rather than re-fetching;
    `data_hash` lets it skip a full JSON diff when nothing changed.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    domain_id: int = Field(foreign_key="domain.id", index=True)
    taken_at: datetime = Field(default_factory=utcnow)
    data: str  # JSON blob: DNS records, RDAP registration, mail posture
    data_hash: str  # hash of `data`, for cheap unchanged-snapshot detection


class DomainQuote(SQLModel, table=True):
    """
    A server-issued, short-lived price quote from a registrar `check` call. `id` is a UUID chosen
    by the caller so it can be handed to the client and referenced by a later purchase without a
    lookup. Money is stored as decimal strings — SQLite has no native Decimal type and float would
    silently corrupt cents.
    """
    id: str = Field(primary_key=True)  # uuid4 hex
    name: str = Field(index=True)
    connection_id: str = Field(foreign_key="connection.id")
    price: str  # first-year price, decimal as str, account currency
    renewal_price: Optional[str] = None  # decimal as str
    currency: str
    premium: bool = False
    expires_at: datetime  # quote TTL (5 min); enforced by the purchase route, not the DB


class DomainPurchase(SQLModel, table=True):
    """
    A purchase intent, inserted as `pending` before the registrar call and updated after. The
    unique `quote_id` is the whole double-submit guard: a retry (client or user) that references
    the same quote fails on insert rather than buying twice.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_id: str = Field(unique=True, index=True, foreign_key="domainquote.id")
    status: str = "pending"  # pending | succeeded | failed | unknown
    price: str  # decimal as str, what was actually charged/attempted
    created_at: datetime = Field(default_factory=utcnow)
    detail: Optional[str] = None  # redacted registrar response/error — never a raw credential


class JobCompany(SQLModel, table=True):
    """
    A company worth polling for job postings — the seed list behind JobBoard resolution. Mirrors
    Domain: a state row, not an event, so `status` can mute a company (no open board found, or the
    user asked to stop watching it) without losing the history of boards/postings resolved against it.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    domain: Optional[str] = Field(default=None, index=True)
    source: str  # watchlist | yc | hn | email | manual
    status: str = "active"  # active | muted
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)


class JobBoard(SQLModel, table=True):
    """
    One resolved ATS board for a company. `resolved_by`/`confidence`/`verified_at` record how the
    resolver ladder (URL pattern -> slug probe -> HTML fingerprint -> LLM web research -> JSON-LD)
    found this board, so the UI can show provenance and let the user correct a low-confidence guess.
    UNIQUE(ats_kind, board_id): the same ATS board is resolved at most once, even if two companies'
    resolution attempts land on the same token/slug.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="jobcompany.id", index=True)
    ats_kind: str  # greenhouse | lever | ashby | workday | smartrecruiters | workable | rippling | bamboohr | recruitee | personio
    board_id: str  # token/site/slug; workday: "tenant|wdN|site"
    resolved_by: str  # pattern | slug_probe | html | llm | manual
    confidence: float
    verified_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    last_error: Optional[str] = None

    __table_args__ = (UniqueConstraint("ats_kind", "board_id"),)


class JobPosting(SQLModel, table=True):
    """
    One posting on one board — state, not an event, like Domain. `first_seen`/`last_seen`/
    `closed_at`/`missed_polls` track its lifecycle without deleting a row that disappears: closed
    means missing from several consecutive polls, not one, to absorb a transient ATS error.
    `content_hash` excludes description whitespace/tracking-param noise (ATS rewrite both on every
    poll) so a refresh can skip a full diff when nothing meaningful changed. UNIQUE(board_id,
    external_id): the ATS's own job id, scoped to the board it came from, dedupes repeated polls.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(foreign_key="jobboard.id", index=True)
    external_id: str  # the ATS's own job id
    title: str
    location: Optional[str] = None
    remote: Optional[bool] = None
    comp_text: Optional[str] = None
    apply_url: str
    content_hash: str  # hash of description/comp with whitespace/tracking noise excluded
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    closed_at: Optional[datetime] = None
    missed_polls: int = 0
    fit_score: Optional[int] = None  # 0-100
    fit_reason: Optional[str] = None

    __table_args__ = (UniqueConstraint("board_id", "external_id"),)


class JobsProfile(SQLModel, table=True):
    """
    The user's own profile for apply assist — contact info, links, work-authorization stance,
    salary/EEO disclosure policy and target preferences. Singleton: the app has exactly one user,
    so callers always operate on `id=1` (created lazily) rather than looking one up by any other
    key. Mirrors DomainPurchase's money-as-decimal-string convention for salary fields.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    other_links: str = "[]"  # JSON list of {label, url} or plain urls
    work_authorized: Optional[bool] = None
    needs_sponsorship: Optional[bool] = None
    open_to_relocation: Optional[bool] = None
    relocation_notes: Optional[str] = None
    start_date_notes: Optional[str] = None  # free text: "2 weeks notice", "immediately", etc.
    salary_floor: Optional[str] = None  # decimal as str, account currency
    # decline | range | exact — what the user is willing to state when a form asks for salary
    salary_disclosure_policy: str = "decline"
    # JSON object of {question: answer}; default policy is decline-to-answer for EEO/demographic
    # questions unless the user explicitly fills one in.
    eeo_answers: str = "{}"
    target_roles: str = "[]"  # JSON list of role titles/keywords
    target_locations: str = "[]"  # JSON list
    target_remote: Optional[bool] = None
    target_salary_floor: Optional[str] = None  # decimal as str
    target_exclusions: str = "[]"  # JSON list of companies/industries to skip
    resume_paths: str = "[]"  # JSON list of file paths, one or more resume PDFs
    cover_letter_tone: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)


class AnswerBank(SQLModel, table=True):
    """
    A screening-question answer the user approved or edited, kept for reuse on later forms.
    `question_norm` is NOT unique on purpose: answers are reused "by similarity" (research doc),
    so more than one stored answer per normalized question is expected as an answer evolves over
    time — a caller picks the best/most recent match itself, this table only stores rows.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    question_norm: str = Field(index=True)  # normalized form of the question, for lookup
    question_raw: str  # as originally asked on the form
    answer: str
    source_application_id: Optional[int] = Field(default=None, foreign_key="jobapplication.id")
    updated_at: datetime = Field(default_factory=utcnow)


class JobApplication(SQLModel, table=True):
    """
    One application against one posting — state, not an event, like Domain/JobPosting. UNIQUE
    posting_id: at most one application per posting. `assist_state` tracks the desktop-runner
    session lifecycle (see §Apply Assist) separately from `status`, the overall application
    lifecycle a user cares about; `assist_log` is the audit trail of what the assist session
    filled, from where, and what the user overrode.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    posting_id: int = Field(unique=True, index=True, foreign_key="jobposting.id")
    # draft | packet_ready | queued | in_progress | submitted | withdrawn | rejected | offer
    status: str = "draft"
    packet: Optional[str] = None  # JSON blob: cover letter text + screening-answer drafts
    cover_letter_path: Optional[str] = None
    resume_path: Optional[str] = None
    assist_state: str = "idle"  # idle | queued | claimed | running | paused | done | failed
    assist_session_id: Optional[str] = Field(default=None, index=True)
    assist_log: Optional[str] = None  # JSON blob: per-step fields filled/values/sources/overrides
    created_at: datetime = Field(default_factory=utcnow)
    submitted_at: Optional[datetime] = None


def _add_missing_columns() -> None:
    """
    Add columns that exist in the models but not yet on disk.

    SQLModel.metadata.create_all() creates missing *tables* and silently no-ops on tables that
    already exist — it never issues ALTER TABLE. data/ is bind-mounted and survives redeploys, so
    without this a model gaining a column makes every query against that table raise
    "no such column" on an existing install, and every run fails until someone edits SQLite by hand.

    Deliberately minimal: adds columns, never drops or retypes them. Anything beyond that wants a
    real migration tool.
    """
    with engine.connect() as conn:
        for table_name, table in SQLModel.metadata.tables.items():
            on_disk = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})")}
            if not on_disk:
                continue  # table doesn't exist yet; create_all will make it complete

            for column in table.columns:
                if column.name in on_disk:
                    continue
                ddl = f"{column.name} {column.type.compile(engine.dialect)}"
                # SQLite refuses a NOT NULL column without a default on a populated table.
                default = getattr(column.default, "arg", None)
                if default is not None and not callable(default):
                    ddl += f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {default}"
                elif not column.nullable:
                    continue  # can't add safely; leave it for a real migration
                conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")
                print(f"[signalslate] migrated: added {table_name}.{column.name}")
        conn.commit()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def get_session() -> Session:
    return Session(engine)


def latest_run() -> Optional[Run]:
    with get_session() as session:
        return session.exec(select(Run).order_by(Run.id.desc())).first()


def list_runs(limit: int = 50) -> list[Run]:
    with get_session() as session:
        return list(session.exec(select(Run).order_by(Run.id.desc()).limit(limit)))


def get_run(run_id: int) -> Optional[Run]:
    with get_session() as session:
        return session.get(Run, run_id)


def source_health_for_run(run_id: int) -> list[SourceHealth]:
    with get_session() as session:
        return list(session.exec(select(SourceHealth).where(SourceHealth.run_id == run_id)))


def items_for_run(run_id: int) -> list[CollectedItem]:
    with get_session() as session:
        return list(session.exec(select(CollectedItem).where(CollectedItem.run_id == run_id)))


def items_for_source_since(source: str, since: datetime) -> list[CollectedItem]:
    """
    One source's items whose own timestamp is at or after `since`, oldest first.

    Not scoped to a run: the triage dry-run wants "what did this inbox receive lately", and overlapping
    collection windows already dedupe on (source, external_id) at insert. Read-only.
    """
    with get_session() as session:
        rows = session.exec(
            select(CollectedItem).where(CollectedItem.source == source).where(CollectedItem.occurred_at >= since)
        ).all()
    # Sorted here, not in SQL: keeps the query free of column-expression calls the type checker cannot see through.
    return sorted(rows, key=lambda row: (row.occurred_at, row.id or 0))


MAX_ITEM_PAGE = 200
MAX_DETAIL_CHARS = 500


def list_items(
    source: str, item_type: Optional[str] = None, before_id: Optional[int] = None, limit: int = 50
) -> list[CollectedItem]:
    """
    One source's items, newest first, one keyset page at a time.

    Keyed on id rather than an offset: collection keeps inserting while the UI pages, and an offset
    would shift under the reader and repeat or skip rows. `before_id` is exclusive, so the last id of
    one page is passed straight back for the next. `limit` is clamped to 1..MAX_ITEM_PAGE.
    """
    limit = max(1, min(limit, MAX_ITEM_PAGE))
    statement = select(CollectedItem).where(CollectedItem.source == source)
    if item_type is not None:
        statement = statement.where(CollectedItem.item_type == item_type)
    if before_id is not None:
        statement = statement.where(col(CollectedItem.id) < before_id)
    statement = statement.order_by(col(CollectedItem.id).desc()).limit(limit)
    with get_session() as session:
        return list(session.exec(statement))


def count_items(source: str, item_type: Optional[str] = None) -> int:
    """How many items one source has stored, optionally narrowed to one item_type."""
    statement = select(func.count()).select_from(CollectedItem).where(CollectedItem.source == source)
    if item_type is not None:
        statement = statement.where(CollectedItem.item_type == item_type)
    with get_session() as session:
        return session.exec(statement).one()


def item_counts_for_run(run_id: int) -> dict[str, int]:
    """{source: count} for one run — feeds the run summary string without loading every payload."""
    counts: dict[str, int] = {}
    for item in items_for_run(run_id):
        counts[item.source] = counts.get(item.source, 0) + 1
    return counts


def existing_external_ids(source: str, external_ids: list[str]) -> set[str]:
    """
    Which of these ids this source has already stored.

    Scoped to the ids being offered rather than everything ever collected, so the query stays small
    as history grows. Collection windows deliberately overlap, so this runs on every insert.
    """
    if not external_ids:
        return set()
    with get_session() as session:
        rows = session.exec(
            select(CollectedItem.external_id)
            .where(CollectedItem.source == source)
            .where(CollectedItem.external_id.in_(external_ids))
        ).all()
    return set(rows)


def get_cursor(source: str) -> Optional[SourceCursor]:
    with get_session() as session:
        return session.get(SourceCursor, source)


def set_cursor(source: str, last_success_at: datetime, cursor: Optional[str] = None) -> None:
    """Advance the watermark and clear the failure streak. Upserts on first collection."""
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is None:
            row = SourceCursor(source=source)
        row.last_success_at = last_success_at
        row.cursor = cursor
        row.consecutive_failures = 0
        session.add(row)
        session.commit()


def record_failure(source: str) -> int:
    """Count one non-clean collection for this source. Returns the new streak length."""
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is None:
            row = SourceCursor(source=source)
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        session.add(row)
        session.commit()
        return row.consecutive_failures


def reset_cursor(source: str, to: Optional[datetime]) -> None:
    """
    Move a source's watermark for a re-backfill. None deletes the row, so the next run collects the
    full window as on first use; a datetime behaves exactly like set_cursor (and clears the streak).
    """
    if to is not None:
        set_cursor(source, to)
        return
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is not None:
            session.delete(row)
            session.commit()


def clear_failures(source: str) -> None:
    """Zero the failure streak only. The watermark stays: the source did not just collect cleanly."""
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is not None:
            row.consecutive_failures = 0
            session.add(row)
            session.commit()


def record_attempt(
    source: str, status: str, detail: Optional[str], item_count: Optional[int], at: Optional[datetime] = None
) -> None:
    """
    Note that a collection was attempted, whatever the outcome.

    Deliberately leaves last_success_at and consecutive_failures alone: those belong to set_cursor and
    record_failure, and a failed attempt must not move the watermark. `detail` is truncated because
    it can carry an upstream error body.
    """
    with get_session() as session:
        row = session.get(SourceCursor, source)
        if row is None:
            row = SourceCursor(source=source)
        row.last_attempt_at = at if at is not None else utcnow()
        row.last_status = status
        row.last_detail = detail[:MAX_DETAIL_CHARS] if detail is not None else None
        row.last_item_count = item_count
        session.add(row)
        session.commit()


def reap_orphaned_runs() -> int:
    """
    Mark any run still flagged "running" at startup as failed. Returns how many.

    execute_run's finally block covers exceptions inside the process, but not a container restart,
    an OOM kill, or a compose down mid-collection. Without this, one such interruption strands a
    row at "running" forever — and since that state hard-blocks every future run (409 from the API,
    a swallowed tick in the scheduler), the digest would stop silently and need hand-edited SQLite
    to recover.
    """
    with get_session() as session:
        stranded = list(session.exec(select(Run).where(Run.status == "running")))
        for run in stranded:
            run.status = "failed"
            run.error = "Interrupted — the process stopped before this run finished."
            run.finished_at = run.finished_at or utcnow()
            session.add(run)
        session.commit()
    return len(stranded)


def has_running_run() -> bool:
    """True if a run is already in flight — the guard against the scheduler and a manual trigger overlapping."""
    with get_session() as session:
        return session.exec(select(Run).where(Run.status == "running")).first() is not None


def latest_source_health() -> list[SourceHealth]:
    """One row per source: the most recent health check recorded for it, across all runs."""
    with get_session() as session:
        rows = session.exec(select(SourceHealth).order_by(SourceHealth.checked_at.desc())).all()
    seen: dict[str, SourceHealth] = {}
    for row in rows:
        seen.setdefault(row.source, row)
    return list(seen.values())
