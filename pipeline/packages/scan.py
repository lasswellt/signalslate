"""
Gmail scan for package-tracking: shipping mail -> extracted shipments -> inventory.

Design decisions:

- Transport is reused from pipeline.collectors.gmail (_call retry/backoff, fetch_message,
  _headers/_walk, parse_internal_date, GET_PACE_SECONDS). Nothing here reimplements HTTP.
- The query is deliberately broad (purchases category, shipping-subject words, carrier senders)
  and filters spam/trash/promotions/social; precision comes from extract, not from Gmail search.
- Raw HTML is walked from the MIME tree rather than taken from flatten_message, which drops
  hrefs that html.harvest needs. Bodies live only in local variables: only extracted fields
  (PackageEmail metadata, PackageEvent rows) are persisted, and no body is ever logged.
- Cursor is a SourceCursor row per account. First run looks back packages_settings().scan_days;
  later runs start one day before last_success_at (Gmail after: boundary and ordering are
  undocumented). The cursor moves only after the account's batch committed, and only to the
  newest message seen, so a page-capped listing never skips unprocessed mail. A message that
  failed to fetch holds the cursor back so the next run retries it.
- Rescans are idempotent: messages already linked in PackageEmail for the account are skipped,
  and inventory dedupes events by hash.
- One account failing (GmailError, requests/token errors) records the exception CLASS NAME only
  (messages may echo response bodies) and the next account still runs.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import select

from pipeline import db, health
from pipeline.clock import utcnow
from pipeline.collectors import gmail
from pipeline.packages import packages_settings
from pipeline.packages import email_status, inventory
from pipeline.packages.extract import MailInput, extract
from pipeline.packages.html import harvest
from pipeline.packages.inventory import MailRef

_log = logging.getLogger(__name__)

SCAN_QUERY = (
    '(category:purchases OR subject:(shipped OR shipment OR delivered OR "out for delivery" '
    'OR "on its way" OR "on the way" OR tracking OR dispatched OR "has been sent" '
    'OR "in transit" OR arrived) OR from:(fedex.com OR ups.com OR usps.com OR dhl.com '
    'OR sefl.com)) -in:spam -in:trash -category:promotions -category:social'
)
CURSOR_PREFIX = "packages_gmail_"
OVERLAP = timedelta(days=1)


@dataclass
class ScanResult:
    """Outcome of one account's scan: messages processed, distinct packages touched, error class names."""
    label: str
    scanned: int = 0
    packages_touched: int = 0
    errors: list[str] = field(default_factory=list)


def list_ids(token: str, query: str, after: datetime) -> list[dict]:
    """
    List {id, threadId} for messages matching `query` received after `after`.

    Args:
        token: Gmail access token.
        query: Gmail search expression.
        after: Naive-UTC lower bound.

    Returns:
        Message stubs, newest first. Stops with a warning at gmail.MAX_PAGES (partial progress).

    Raises:
        gmail.GmailError: A page request failed after retries.
    """
    params = {"q": f"{query} after:{gmail.to_epoch_seconds(after)}", "maxResults": gmail.PAGE_SIZE}
    out: list[dict] = []
    page_token: Optional[str] = None
    for _ in range(gmail.MAX_PAGES):
        page = {**params, "pageToken": page_token} if page_token else dict(params)
        body = gmail._call(token, "/messages", page)
        out.extend(body.get("messages") or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            return out
    _log.warning("packages scan: listing capped at %d pages, processing partial results", gmail.MAX_PAGES)
    return out


def _bodies(msg: dict) -> tuple[Optional[str], Optional[str]]:
    """(html, plain) raw text of a messages.get payload; None when a flavour is absent."""
    plain: list[str] = []
    html: list[str] = []
    gmail._walk(msg.get("payload") or {}, plain, html, [])
    return ("\n".join(html)[: gmail.MAX_HTML_CHARS] or None), ("\n".join(plain) or None)


def _since(session, source: str, now: datetime, scan_days: int) -> datetime:
    """Start of the scan window: cursor minus overlap, or scan_days back on the first run."""
    cursor = session.get(db.SourceCursor, source)
    if cursor is not None and cursor.last_success_at is not None:
        return cursor.last_success_at - OVERLAP
    return now - timedelta(days=scan_days)


def _scan_account(label: str, now: datetime) -> ScanResult:
    """Scan one account inside a single session; raises on account-level failure."""
    result = ScanResult(label=label)
    settings = packages_settings()
    source = f"{CURSOR_PREFIX}{label}"
    token = health.gmail_token_response(label)["access_token"]
    with db.get_session() as session:
        try:
            since = _since(session, source, now, settings.scan_days)
            stubs = list_ids(token, SCAN_QUERY, since)
            touched: set[int] = set()
            newest: Optional[datetime] = None
            fetch_failed = False
            for i, stub in enumerate(stubs):
                message_id = stub.get("id")
                if not message_id:
                    continue
                if i:
                    gmail._sleep(gmail.GET_PACE_SECONDS)
                try:
                    msg = gmail.fetch_message(token, message_id)
                except gmail.GmailError as exc:
                    fetch_failed = True
                    result.errors.append(type(exc).__name__)
                    continue
                received = gmail.parse_internal_date(msg.get("internalDate")) or now
                if newest is None or received > newest:
                    newest = received
                result.scanned += 1
                known = session.exec(select(db.PackageEmail.id).where(
                    db.PackageEmail.account_label == label,
                    db.PackageEmail.message_id == message_id)).first()
                if known is not None:
                    continue
                headers = gmail._headers((msg.get("payload") or {}).get("headers"))
                sender = headers.get("from") or ""
                subject = headers.get("subject") or ""
                html, plain = _bodies(msg)
                shipments = extract(MailInput(sender=sender, subject=subject, html=html,
                                              plain=plain, received_at=received))
                if not shipments:
                    continue
                text = harvest(html, plain).text
                signal = email_status.classify_status(sender, subject, text, received)
                mail = MailRef(account_label=label, message_id=message_id,
                               thread_id=msg.get("threadId"), subject=subject or None,
                               sender=sender or None, received_at=received)
                touched.update(p.id for p in inventory.apply_email(session, shipments, signal, mail)
                               if p.id is not None)
            result.packages_touched = len(touched)

            if not fetch_failed:
                cursor = session.get(db.SourceCursor, source) or db.SourceCursor(source=source)
                cursor.last_success_at = min(newest, now) if newest else now
                cursor.last_attempt_at = now
                cursor.last_status = "ok"
                session.add(cursor)
            session.commit()
        except Exception:
            session.rollback()
            raise
    return result


def scan_account(label: str, refresh_token: Optional[str], now: datetime) -> ScanResult:
    """
    Scan one Gmail account for shipping mail and apply it to the package inventory.

    Args:
        label: Lowercased account label.
        refresh_token: The account's refresh token; None/empty records a MissingRefreshToken error.
        now: Naive-UTC scan time.

    Returns:
        ScanResult; failures are recorded as exception class names, never raised.
    """
    if not refresh_token:
        return ScanResult(label=label, errors=["MissingRefreshToken"])
    try:
        return _scan_account(label, now)
    except Exception as exc:  # one account must not stop the next; class name only, no message
        _log.warning("packages scan: account %s failed: %s", label, type(exc).__name__)
        return ScanResult(label=label, errors=[type(exc).__name__])


def scan_all(now: Optional[datetime] = None) -> list[ScanResult]:
    """
    Scan every configured Gmail account, then mark long-silent packages stale.

    Args:
        now: Naive-UTC scan time; defaults to the current time.

    Returns:
        One ScanResult per account, in label order.
    """
    moment = now or utcnow()
    results = [scan_account(label, token, moment) for label, token in health.gmail_accounts().items()]
    with db.get_session() as session:
        inventory.mark_stale(session, moment, packages_settings().stale_days)
        session.commit()
    return results
