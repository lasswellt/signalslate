"""
Package tracking routes (docs/plans/package-tracking): list, detail, manual add, archive, delete,
Gmail scan, carrier refresh, carrier availability and the Home Assistant summary.

Design decisions:

- Reads query pipeline.db.Package/PackageEvent/PackageEmail directly (the api.routers.domains
  pattern); mutations go through pipeline.packages.inventory so identity rules live in one place.
- Static paths (/packages/summary, /packages/carriers, /packages/scan, /packages/refresh) are
  declared before /packages/{package_id}; the id is an int, so a stray word is a 422, not a lookup.
- "Today" is the local calendar day in api.scheduler.schedule_timezone() (UTC when TZ is unset),
  the same zone the cron schedules use. Timestamps (delivered_at) are naive UTC and converted per
  row; eta is a calendar date stored as naive midnight, so its .date() is compared directly
  (converting it would shift it a day west of UTC, and the digest collector also uses eta.date()).
- add_package relies on inventory.add_manual's (package, created) result and the unique index:
  a concurrent insert surfaces as IntegrityError and becomes the same 409 package_exists.
- list_packages loads the latest event of all listed packages in one grouped query (no N+1).
- Detail emails expose metadata only (account, subject, sender, time); bodies are never stored, so
  they cannot be returned.
- scan/refresh are blocking network calls, so every handler is plain `def` (threadpool). An
  unexpected exception becomes a coded 502 naming only the exception class, never its text.
- /packages/carriers reports whether each carrier API is configured and nothing else: no ids,
  no secrets.
- pipeline.packages modules are imported by module so tests can monkeypatch scan_all/refresh_due
  at this file's own import site.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, delete, select

from api.scheduler import schedule_timezone
from api.serialize import iso_z
from pipeline import db
from pipeline.clock import utcnow
from pipeline.db import Package, PackageEmail, PackageEvent
from pipeline.packages import inventory, links, packages_settings, poll
from pipeline.packages import scan as package_scan

router = APIRouter(tags=["packages"])

_log = logging.getLogger(__name__)

# Lower sorts first: active shipments, then everything else, then delivered.
_STATUS_ORDER = {"out_for_delivery": 0, "in_transit": 1, "pending": 2, "exception": 3, "delivered": 5}
_OTHER_ORDER = 4


class PackageOut(BaseModel):
    id: int
    carrier: str
    tracking_number: Optional[str]
    merchant: Optional[str]
    order_id: Optional[str]
    status: str
    status_source: str
    eta: Optional[str]
    delivered_at: Optional[str]
    last_event_at: Optional[str]
    last_checked_at: Optional[str]
    last_error: Optional[str]
    archived: bool
    tracking_url: Optional[str]
    last_event: Optional[str]


class EventOut(BaseModel):
    occurred_at: Optional[str]
    status: str
    description: Optional[str]
    location: Optional[str]
    source: str


class EmailOut(BaseModel):
    account_label: str
    subject: Optional[str]
    sender: Optional[str]
    received_at: Optional[str]


class PackageDetailOut(PackageOut):
    events: list[EventOut]
    emails: list[EmailOut]


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking_number: str = Field(min_length=4, max_length=64)
    carrier: Optional[str] = Field(default=None, max_length=32)

    @field_validator("tracking_number")
    @classmethod
    def _charset(cls, value: str) -> str:
        if not all(c.isascii() and (c.isalnum() or c in " -") for c in value):
            raise ValueError("tracking number may contain only letters, digits, spaces and hyphens")
        return value


class PatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archived: bool


class SummaryOut(BaseModel):
    out_for_delivery_today: int
    arriving_today: int
    delivered_today: int
    in_transit: int
    exceptions: int
    generated_at: str


class ScanOut(BaseModel):
    label: str
    scanned: int
    packages_touched: int
    errors: list[str]


class RefreshOut(BaseModel):
    polled: int
    changed: int
    errors: dict[str, str]


class CarrierOut(BaseModel):
    code: str
    configured: bool


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _last_event(session, package_id: int) -> Optional[PackageEvent]:
    return session.exec(select(PackageEvent).where(PackageEvent.package_id == package_id)
                        .order_by(col(PackageEvent.occurred_at).desc(), col(PackageEvent.id).desc())).first()


def _last_events(session, package_ids: list[int]) -> dict[int, PackageEvent]:
    """Latest event (occurred_at, then id) per package id in one grouped query."""
    if not package_ids:
        return {}
    newest = (select(PackageEvent.package_id, func.max(PackageEvent.occurred_at).label("newest"))
              .where(col(PackageEvent.package_id).in_(package_ids))
              .group_by(PackageEvent.package_id).subquery())
    stmt = select(PackageEvent).join(newest, and_(PackageEvent.package_id == newest.c.package_id,
                                                  PackageEvent.occurred_at == newest.c.newest))
    latest: dict[int, PackageEvent] = {}
    for event in session.exec(stmt):
        current = latest.get(event.package_id)
        if current is None or event.id > current.id:
            latest[event.package_id] = event
    return latest


def _to_out(package: Package, last: Optional[PackageEvent]) -> PackageOut:
    return PackageOut(
        id=package.id, carrier=package.carrier, tracking_number=package.tracking_number,
        merchant=package.merchant, order_id=package.order_id, status=package.status,
        status_source=package.status_source, eta=iso_z(package.eta),
        delivered_at=iso_z(package.delivered_at), last_event_at=iso_z(package.last_event_at),
        last_checked_at=iso_z(package.last_checked_at), last_error=package.last_error,
        archived=package.archived,
        tracking_url=links.tracking_url(package.carrier, package.tracking_number,
                                        merchant_domain=package.merchant_domain,
                                        order_id=package.order_id),
        last_event=last.description if last is not None else None,
    )


def _get_or_404(session, package_id: int) -> Package:
    package = session.get(Package, package_id)
    if package is None:
        raise _coded(404, "package_not_found", "Package not found")
    return package


def _sort_key(package: Package) -> tuple:
    rank = _STATUS_ORDER.get(package.status, _OTHER_ORDER)
    stamp = (package.delivered_at or package.last_event_at or package.created_at or datetime.min)
    return (rank, -stamp.replace(tzinfo=timezone.utc).timestamp(), -(package.id or 0))


@router.get("/packages", response_model=list[PackageOut])
def list_packages(status: Optional[str] = None, carrier: Optional[str] = None,
                  include_archived: bool = False,
                  q: Optional[str] = Query(default=None, max_length=100)) -> list[PackageOut]:
    """List packages, active first then delivered newest first.

    Args:
        status: Keep only this status.
        carrier: Keep only this carrier code.
        include_archived: Include archived packages.
        q: Case-insensitive substring over tracking number, merchant and order id.

    Returns:
        PackageOut rows in display order.
    """
    with db.get_session() as session:
        stmt = select(Package)
        if status:
            stmt = stmt.where(Package.status == status)
        if carrier:
            stmt = stmt.where(Package.carrier == carrier.strip().lower())
        if not include_archived:
            stmt = stmt.where(col(Package.archived).is_(False))
        rows = list(session.exec(stmt))
        needle = (q or "").strip().lower()
        if needle:
            rows = [p for p in rows if any(needle in (v or "").lower()
                                           for v in (p.tracking_number, p.merchant, p.order_id))]
        rows.sort(key=_sort_key)
        latest = _last_events(session, [p.id for p in rows])
        return [_to_out(p, latest.get(p.id)) for p in rows]


def _local_date(dt: Optional[datetime], zone):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(zone).date()


@router.get("/packages/summary", response_model=SummaryOut)
def packages_summary() -> SummaryOut:
    """Counts for the Home Assistant sensor, bucketed by the local calendar day.

    Returns:
        out_for_delivery_today, arriving_today (ETA is today and not yet delivered),
        delivered_today, in_transit, exceptions (non-archived packages only).
    """
    now = utcnow()
    zone = schedule_timezone() or timezone.utc
    today = _local_date(now, zone)
    with db.get_session() as session:
        rows = list(session.exec(select(Package).where(col(Package.archived).is_(False))))
    return SummaryOut(
        out_for_delivery_today=sum(1 for p in rows if p.status == "out_for_delivery"),
        arriving_today=sum(1 for p in rows if p.status not in ("delivered", "returned")
                           and p.eta is not None and p.eta.date() == today),
        delivered_today=sum(1 for p in rows if p.status == "delivered"
                            and _local_date(p.delivered_at, zone) == today),
        in_transit=sum(1 for p in rows if p.status == "in_transit"),
        exceptions=sum(1 for p in rows if p.status == "exception"),
        generated_at=iso_z(now),
    )


@router.get("/packages/carriers", response_model=list[CarrierOut])
def carriers() -> list[CarrierOut]:
    """Which carrier tracking APIs are configured (never the credentials).

    Returns:
        One entry per supported API carrier.
    """
    settings = packages_settings()
    return [CarrierOut(code="ups", configured=settings.ups_enabled),
            CarrierOut(code="usps", configured=settings.usps_enabled)]


@router.post("/packages/scan", response_model=list[ScanOut])
def scan_packages() -> list[ScanOut]:
    """Scan every connected Gmail account now.

    Returns:
        Per-account scan outcome.

    Raises:
        HTTPException: 502 scan_failed on an unexpected error (exception class only).
    """
    try:
        results = package_scan.scan_all()
    except Exception as exc:
        _log.warning("package scan failed: %s", type(exc).__name__)
        raise _coded(502, "scan_failed", f"Scan failed ({type(exc).__name__})")
    return [ScanOut(label=r.label, scanned=r.scanned, packages_touched=r.packages_touched,
                    errors=list(r.errors)) for r in results]


@router.post("/packages/refresh", response_model=RefreshOut)
def refresh_packages() -> RefreshOut:
    """Poll carrier APIs for every due package now.

    Returns:
        polled, changed and per-carrier errors.

    Raises:
        HTTPException: 502 refresh_failed on an unexpected error (exception class only).
    """
    try:
        result = poll.refresh_due()
    except Exception as exc:
        _log.warning("package refresh failed: %s", type(exc).__name__)
        raise _coded(502, "refresh_failed", f"Refresh failed ({type(exc).__name__})")
    return RefreshOut(polled=result.polled, changed=result.changed, errors=dict(result.errors))


@router.post("/packages", response_model=PackageOut, status_code=201)
def add_package(body: AddBody) -> PackageOut:
    """Add a package by tracking number, detecting the carrier when none is given.

    Args:
        body: tracking_number and optional carrier code.

    Returns:
        The created package.

    Raises:
        HTTPException: 422 unrecognized_tracking_number, 409 package_exists (also when archived).
    """
    exists = _coded(409, "package_exists", "Package already tracked")
    with db.get_session() as session:
        try:
            package, created = inventory.add_manual(session, body.tracking_number, body.carrier)
            if created:
                session.commit()
        except ValueError:
            raise _coded(422, "unrecognized_tracking_number",
                         "Tracking number not recognised; specify the carrier")
        except IntegrityError:
            session.rollback()
            _log.info("package add lost an insert race")
            package, created = _existing(session, body), False
        if not created:
            if package is not None and package.archived:
                raise _coded(409, "package_exists", "Package already tracked (archived)")
            raise exists
        session.refresh(package)
        return _to_out(package, None)


def _existing(session, body: AddBody) -> Optional[Package]:
    """The row a lost insert race collided with, for the archived-aware 409 message."""
    number = "".join(body.tracking_number.split()).upper()
    return session.exec(select(Package).where(Package.tracking_number == number)).first()


@router.get("/packages/{package_id}", response_model=PackageDetailOut)
def package_detail(package_id: int) -> PackageDetailOut:
    """Package with its event timeline (newest first) and linked email metadata.

    Args:
        package_id: Package row id.

    Raises:
        HTTPException: 404 package_not_found.
    """
    with db.get_session() as session:
        package = _get_or_404(session, package_id)
        events = list(session.exec(select(PackageEvent).where(PackageEvent.package_id == package_id)
                                   .order_by(col(PackageEvent.occurred_at).desc(),
                                             col(PackageEvent.id).desc())))
        emails = list(session.exec(select(PackageEmail).where(PackageEmail.package_id == package_id)
                                   .order_by(col(PackageEmail.received_at).desc())))
        base = _to_out(package, events[0] if events else None)
        return PackageDetailOut(
            **base.model_dump(),
            events=[EventOut(occurred_at=iso_z(e.occurred_at), status=e.status,
                             description=e.description, location=e.location, source=e.source)
                    for e in events],
            emails=[EmailOut(account_label=m.account_label, subject=m.subject, sender=m.sender,
                             received_at=iso_z(m.received_at)) for m in emails],
        )


@router.patch("/packages/{package_id}", response_model=PackageOut)
def patch_package(package_id: int, body: PatchBody) -> PackageOut:
    """Archive or unarchive a package.

    Args:
        package_id: Package row id.
        body: archived flag.

    Raises:
        HTTPException: 404 package_not_found.
    """
    with db.get_session() as session:
        package = _get_or_404(session, package_id)
        package.archived = body.archived
        package.updated_at = utcnow()
        session.add(package)
        session.commit()
        session.refresh(package)
        return _to_out(package, _last_event(session, package.id))


@router.delete("/packages/{package_id}", status_code=204)
def delete_package(package_id: int) -> Response:
    """Delete a package with its events and email links.

    Args:
        package_id: Package row id.

    Raises:
        HTTPException: 404 package_not_found.
    """
    with db.get_session() as session:
        _get_or_404(session, package_id)
        session.exec(delete(PackageEvent).where(PackageEvent.package_id == package_id))
        session.exec(delete(PackageEmail).where(PackageEmail.package_id == package_id))
        session.exec(delete(Package).where(Package.id == package_id))
        session.commit()
    return Response(status_code=204)
