"""
Carrier polling: refresh UPS/USPS packages that are due.

Design decisions:

- Only carriers with a configured adapter are polled (carriers.base.adapters()); walmart, amazon,
  sefl, unknown and order-level packages (no tracking number) have no adapter or number and are
  never selected.
- Due = tracking number present, not archived, not returned, not delivered longer than
  stop_after_delivered_hours ago, and next_check_at empty or <= now. Oldest next_check_at first
  so a rate-limited run starves nobody.
- Cadence: a successful poll sets next_check_at = now + poll_interval_hours. Not-found backs off
  12h because carriers can take a while to know a fresh number.
- Rate limiting and auth failures stop that carrier for the rest of the run (other carriers keep
  going). Rate limiting leaves next_check_at alone so the next run retries; auth errors touch no
  package and surface as "not authorized" in the per-carrier errors map.
- Nothing is raised to the scheduler. Logs carry exception class names only. One commit per
  package so a later failure never loses earlier progress.
- adapters_override lets tests inject real adapter objects implementing the CarrierAdapter protocol.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import select

from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import Package
from pipeline.packages import (DEFAULT_POLL_INTERVAL_HOURS, DEFAULT_STOP_AFTER_DELIVERED_HOURS,
                               inventory)
from pipeline.packages.carriers import base
from pipeline.packages.carriers.base import (CarrierAdapter, CarrierAuthError, CarrierError,
                                             CarrierNotFound, CarrierRateLimited)

_log = logging.getLogger(__name__)

NOT_FOUND_BACKOFF_HOURS = 12


@dataclass
class PollResult:
    """Outcome of one refresh_due() run."""
    polled: int = 0
    changed: int = 0
    errors: dict[str, str] = field(default_factory=dict)


def _is_due(package: Package, now: datetime, stop_after: timedelta) -> bool:
    if package.status == "returned":
        return False
    if package.status == "delivered":
        since = package.delivered_at or package.last_event_at or package.updated_at
        if since is not None and now - since > stop_after:
            return False
    return True


def refresh_due(now: Optional[datetime] = None, *,
                adapters_override: Optional[dict[str, CarrierAdapter]] = None) -> PollResult:
    """
    Poll every due package whose carrier has a configured adapter.

    Args:
        now: Reference time (naive UTC); defaults to utcnow().
        adapters_override: Carrier code to adapter, replacing carriers.base.adapters() (tests).

    Returns:
        PollResult with packages polled, how many changed status, and per-carrier error text.
        Never raises carrier errors.
    """
    result = PollResult()
    now = now or utcnow()
    try:
        adapters = dict(adapters_override) if adapters_override is not None else base.adapters()
    except Exception as exc:  # adapter construction must never break the scheduler
        _log.warning("packages poll: adapters unavailable (%s)", type(exc).__name__)
        return result
    if not adapters:
        return result

    interval = timedelta(hours=DEFAULT_POLL_INTERVAL_HOURS)
    stop_after = timedelta(hours=DEFAULT_STOP_AFTER_DELIVERED_HOURS)
    stopped: set[str] = set()

    with db.get_session() as session:
        candidates = session.exec(
            select(Package)
            .where(Package.carrier.in_(list(adapters)))
            .where(Package.tracking_number.is_not(None))
            .where(Package.archived == False)  # noqa: E712 - SQL comparison
            .where((Package.next_check_at.is_(None)) | (Package.next_check_at <= now))
            .order_by(Package.next_check_at.asc(), Package.id.asc())
        ).all()
        ids = [p.id for p in candidates if _is_due(p, now, stop_after)]

        for package_id in ids:
            package = session.get(Package, package_id)
            if package is None or package.carrier in stopped:
                continue
            carrier = package.carrier
            try:
                track = adapters[carrier].track(package.tracking_number)
                if inventory.apply_carrier_result(session, package, track):
                    result.changed += 1
                package.last_checked_at = now
                package.next_check_at = now + interval
                package.last_error = None
                result.polled += 1
            except CarrierRateLimited as exc:
                session.rollback()
                stopped.add(carrier)
                result.errors[carrier] = "rate limited"
                _log.warning("packages poll: %s %s, stopping carrier for this run",
                             carrier, type(exc).__name__)
                continue
            except CarrierAuthError as exc:
                session.rollback()
                stopped.add(carrier)
                result.errors[carrier] = "not authorized"
                _log.warning("packages poll: %s %s, stopping carrier for this run",
                             carrier, type(exc).__name__)
                continue
            except CarrierNotFound:
                session.rollback()
                package = session.get(Package, package_id)
                package.last_checked_at = now
                package.last_error = "not found"
                package.next_check_at = now + timedelta(hours=NOT_FOUND_BACKOFF_HOURS)
                result.polled += 1
            except Exception as exc:  # CarrierError and anything unexpected; never reach scheduler
                session.rollback()
                name = type(exc).__name__
                if not isinstance(exc, CarrierError):
                    _log.warning("packages poll: unexpected %s for %s", name, carrier)
                package = session.get(Package, package_id)
                package.last_checked_at = now
                package.last_error = name
                package.next_check_at = now + interval
                result.errors[carrier] = name
                result.polled += 1
            session.add(package)
            session.commit()
    return result
