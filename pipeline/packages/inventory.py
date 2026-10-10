"""
Package inventory: the DB layer that turns extracted shipments and status signals into Package rows.

Design decisions:

- Identity is (carrier, tracking_number); order-level packages (no number yet) are identified by
  (merchant_domain, order_id). A numbered shipment upgrades a matching order-level package in place
  rather than creating a duplicate. Gmail thread ids are stored for display only and are never used
  to correlate: one thread can carry several unrelated orders (Validation finding 3).
- An order-only shipment (no tracking number) is applied only when a status signal exists, so
  marketing or noise mail from a merchant can never create a package. A numbered shipment is itself
  evidence and is created even without a signal (status pending).
- Status moves through can_transition(): ranked statuses only go forward, exception may follow any
  non-terminal status, delivered/returned are terminal, stale/unknown never overwrite anything.
  Events are always recorded (deduplicated by hash), even when the status does not move, so a late
  older email still enriches the timeline without demoting the package.
- Event dedupe is a sha256 over package|status|occurred_at|source with a pre-check, backed by the
  UNIQUE column, so re-scanning the same mail or re-polling the same carrier scan inserts nothing.
- Only extracted fields are persisted (subject truncated to 300); email bodies never reach this
  module. Nothing here commits: the caller owns the transaction (session.flush is used for ids).
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Optional, Protocol

from sqlmodel import Session, select

from pipeline.clock import utcnow
from pipeline.db import Package, PackageEmail, PackageEvent
from pipeline.packages import SIDE_STATUSES, STATUS_RANK, TERMINAL_STATUSES
from pipeline.packages import numbers
from pipeline.packages.email_status import StatusSignal
from pipeline.packages.extract import Shipment

_log = logging.getLogger(__name__)

_SUBJECT_MAX = 300


@dataclass(frozen=True)
class MailRef:
    """Metadata of the Gmail message a shipment was extracted from (never its body)."""
    account_label: str
    message_id: str
    thread_id: Optional[str]
    subject: Optional[str]
    sender: Optional[str]
    received_at: datetime


class CarrierEventLike(Protocol):
    """One carrier scan event."""
    occurred_at: datetime
    status: str
    description: Optional[str]
    location: Optional[str]


class CarrierResultLike(Protocol):
    """What a carrier client returns for one tracking number."""
    status: str
    events: Iterable[CarrierEventLike]
    eta: Optional[datetime]
    delivered_at: Optional[datetime]


def can_transition(current: str, new: str) -> bool:
    """
    Whether a package in status `current` may move to `new`.

    Args:
        current: The package's present status.
        new: The proposed status.

    Returns:
        True when the move is allowed by the monotonic-status rules.
    """
    if new == current or new in ("stale", "unknown") or current in TERMINAL_STATUSES:
        return False
    if new == "exception" or new == "returned":
        return True
    if new not in STATUS_RANK:
        return False
    if current in STATUS_RANK:
        return STATUS_RANK[new] > STATUS_RANK[current]
    return current in SIDE_STATUSES  # exception/stale/unknown are replaced by any ranked status


def _event_hash(package_id: int, status: str, occurred_at: datetime, source: str) -> str:
    raw = f"{package_id}|{status}|{occurred_at.isoformat()}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _add_event(session: Session, package: Package, status: str, occurred_at: datetime,
               description: Optional[str], location: Optional[str], source: str) -> bool:
    """Insert a PackageEvent unless an identical one exists; True when inserted."""
    digest = _event_hash(package.id, status, occurred_at, source)
    if session.exec(select(PackageEvent.id).where(PackageEvent.dedupe_hash == digest)).first() is not None:
        return False
    session.add(PackageEvent(package_id=package.id, occurred_at=occurred_at, status=status,
                             description=description, location=location, source=source,
                             dedupe_hash=digest))
    session.flush()
    return True


def _move(package: Package, new_status: str, occurred_at: datetime, source: str,
          eta: Optional[datetime], delivered_at: Optional[datetime] = None) -> bool:
    """Apply a status observation to the package row; True when the status changed."""
    changed = can_transition(package.status, new_status)
    if changed:
        package.status = new_status
        if new_status == "delivered":
            package.delivered_at = delivered_at or occurred_at
    package.status_source = source if changed else package.status_source
    if eta is not None:
        package.eta = eta
    if package.last_event_at is None or occurred_at > package.last_event_at:
        package.last_event_at = occurred_at
    package.updated_at = utcnow()
    return changed


def _find_numbered(session: Session, carrier: str, number: str) -> Optional[Package]:
    return session.exec(select(Package).where(Package.carrier == carrier,
                                               Package.tracking_number == number)).first()


def _order_packages(session: Session, domain: Optional[str], order_id: Optional[str]) -> list[Package]:
    if not domain or not order_id:
        return []
    return list(session.exec(select(Package).where(Package.merchant_domain == domain,
                                                   Package.order_id == order_id).order_by(Package.id)))


def _fill_identity(package: Package, shipment: Shipment) -> None:
    for attr in ("merchant", "merchant_domain", "order_id"):
        if getattr(package, attr) is None and getattr(shipment, attr) is not None:
            setattr(package, attr, getattr(shipment, attr))


def _link_mail(session: Session, package: Package, mail: MailRef) -> None:
    existing = session.exec(select(PackageEmail.id).where(
        PackageEmail.account_label == mail.account_label,
        PackageEmail.message_id == mail.message_id,
        PackageEmail.package_id == package.id)).first()
    if existing is None:
        session.add(PackageEmail(package_id=package.id, account_label=mail.account_label,
                                 message_id=mail.message_id, thread_id=mail.thread_id,
                                 subject=(mail.subject or "")[:_SUBJECT_MAX] or None,
                                 sender=mail.sender, received_at=mail.received_at))
        session.flush()


def _resolve(session: Session, shipment: Shipment) -> list[Package]:
    """Find or create the Package rows a shipment refers to (never by thread id)."""
    if shipment.tracking_number is not None:
        found = _find_numbered(session, shipment.carrier, shipment.tracking_number)
        if found is not None:
            _fill_identity(found, shipment)
            return [found]
        for candidate in _order_packages(session, shipment.merchant_domain, shipment.order_id):
            if candidate.tracking_number is None:  # upgrade the order-level row in place
                candidate.carrier = shipment.carrier
                candidate.tracking_number = shipment.tracking_number
                _fill_identity(candidate, shipment)
                session.flush()
                return [candidate]
        package = Package(carrier=shipment.carrier, tracking_number=shipment.tracking_number,
                          merchant=shipment.merchant, merchant_domain=shipment.merchant_domain,
                          order_id=shipment.order_id, status="pending", status_source="email")
    else:
        matches = _order_packages(session, shipment.merchant_domain, shipment.order_id)
        if matches:
            return matches
        if not shipment.merchant_domain or not shipment.order_id:
            return []
        package = Package(carrier=shipment.carrier, merchant=shipment.merchant,
                          merchant_domain=shipment.merchant_domain, order_id=shipment.order_id,
                          status="pending", status_source="email")
    session.add(package)
    session.flush()
    return [package]


def apply_email(session: Session, shipments: list[Shipment], signal: Optional[StatusSignal],
                mail: MailRef) -> list[Package]:
    """
    Record the shipments found in one email and the status the email conveys.

    Args:
        session: Open session; the caller commits.
        shipments: Shipments extracted from the mail.
        signal: Status inferred from the mail, or None when it carries none.
        mail: Message metadata for the PackageEmail link.

    Returns:
        The distinct Package rows the mail touched (created, upgraded or updated).
    """
    touched: dict[int, Package] = {}
    for shipment in shipments:
        if shipment.tracking_number is None and signal is None:
            continue  # order-only mail without a status signal is noise
        for package in _resolve(session, shipment):
            if package.id in touched:
                continue
            touched[package.id] = package
            if signal is not None:
                eta = datetime.combine(signal.eta, time.min) if signal.eta else None
                _add_event(session, package, signal.status, signal.occurred_at,
                           signal.description, None, "email")
                _move(package, signal.status, signal.occurred_at, "email", eta)
            package.updated_at = utcnow()
            _link_mail(session, package, mail)
    session.flush()
    return list(touched.values())


def apply_carrier_result(session: Session, package: Package, result: CarrierResultLike) -> bool:
    """
    Apply a carrier poll result to a package.

    Args:
        session: Open session; the caller commits.
        package: The package that was polled (must be persisted).
        result: Carrier status, scan events, ETA and delivery time.

    Returns:
        True when the package status changed.
    """
    for event in result.events:
        _add_event(session, package, event.status, event.occurred_at,
                   event.description, event.location, "carrier")
    now = utcnow()
    stamps = [e.occurred_at for e in result.events]
    occurred = max(stamps) if stamps else (result.delivered_at or now)
    changed = _move(package, result.status, occurred, "carrier", result.eta, result.delivered_at)
    package.last_checked_at = now
    session.add(package)
    session.flush()
    return changed


def add_manual(session: Session, tracking_number: str, carrier: Optional[str]) -> Package:
    """
    Add a package the user typed in.

    Args:
        session: Open session; the caller commits.
        tracking_number: The number as entered.
        carrier: Carrier code, or None to detect it from the number's format.

    Returns:
        The new package, or the existing one for the same (carrier, number).

    Raises:
        ValueError: when no carrier is given and no known format matches the number.
    """
    number = "".join(tracking_number.split()).upper()
    if not number:
        raise ValueError("tracking number is empty")
    code = carrier.strip().lower() if carrier and carrier.strip() else None
    if code is None:
        match = numbers.best_match(number, context="", labeled=True)
        if match is None:
            raise ValueError("unrecognised tracking number format; specify the carrier")
        code = match.carrier
    existing = _find_numbered(session, code, number)
    if existing is not None:
        return existing
    package = Package(carrier=code, tracking_number=number, status="pending", status_source="manual")
    session.add(package)
    session.flush()
    return package


def mark_stale(session: Session, now: datetime, stale_days: int) -> int:
    """
    Mark non-terminal, non-archived packages with no recent event as stale.

    Args:
        session: Open session; the caller commits.
        now: Reference time (naive UTC).
        stale_days: Days without an event (or since creation) after which a package is stale.

    Returns:
        Number of packages marked stale.
    """
    cutoff = now - timedelta(days=stale_days)
    count = 0
    for package in session.exec(select(Package).where(Package.archived == False)):  # noqa: E712
        if package.status in TERMINAL_STATUSES or package.status == "stale":
            continue
        if (package.last_event_at or package.created_at) < cutoff:
            package.status = "stale"
            package.updated_at = now
            count += 1
    session.flush()
    return count


def archive(session: Session, package_id: int, archived: bool) -> Optional[Package]:
    """
    Set or clear the archived flag.

    Args:
        session: Open session; the caller commits.
        package_id: Package primary key.
        archived: New flag value.

    Returns:
        The package, or None when it does not exist.
    """
    package = session.get(Package, package_id)
    if package is None:
        return None
    package.archived = archived
    package.updated_at = utcnow()
    session.add(package)
    session.flush()
    return package


def delete(session: Session, package_id: int) -> bool:
    """
    Delete a package with its events and email links.

    Args:
        session: Open session; the caller commits.
        package_id: Package primary key.

    Returns:
        True when a package was deleted.
    """
    package = session.get(Package, package_id)
    if package is None:
        return False
    for row in session.exec(select(PackageEvent).where(PackageEvent.package_id == package_id)):
        session.delete(row)
    for row in session.exec(select(PackageEmail).where(PackageEmail.package_id == package_id)):
        session.delete(row)
    session.delete(package)
    session.flush()
    return True
