"""
Digest collector for the "packages" source: turns tracked Package rows into digest Items.

Design decisions:
- Local DB only. No network, no credentials: carrier polling and the Gmail scan (separate
  scheduled jobs) already populated the Package table, so check_packages() in pipeline.health is
  a plain row count with nothing external to fail.
- Three item kinds over the digest window [since, until): packages delivered in the window
  (delivered_at), packages whose status is out_for_delivery with last_event_at in the window, and
  non-terminal packages whose eta falls on the window's end date ("arriving today").
- external_id is f"package:{id}:{status}" so a rerun over the same window dedupes, and a package
  that moves on (e.g. out_for_delivery -> delivered) yields a fresh digest item.
- A package matching several rules yields one Item per distinct status (OFD and arriving-today
  share the status, so they collapse to one).
- Archived packages are excluded. Email bodies are never read or stored here.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlmodel import select

from pipeline import db
from pipeline.collectors import CollectionResult, Item
from pipeline.db import Package
from pipeline.packages import TERMINAL_STATUSES
from pipeline.packages.links import tracking_url

_log = logging.getLogger(__name__)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _item(package: Package, occurred_at: datetime) -> Item:
    return Item(
        item_type="package",
        external_id=f"package:{package.id}:{package.status}",
        occurred_at=occurred_at,
        payload={
            "carrier": package.carrier,
            "tracking_number": package.tracking_number,
            "merchant": package.merchant,
            "order_id": package.order_id,
            "status": package.status,
            "eta": _iso(package.eta),
            "delivered_at": _iso(package.delivered_at),
            "tracking_url": tracking_url(
                package.carrier,
                package.tracking_number,
                merchant_domain=package.merchant_domain,
                order_id=package.order_id,
            ),
            "last_error": package.last_error,
        },
    )


def collect_packages(since: datetime, until: datetime) -> CollectionResult:
    """
    Collects delivered, out-for-delivery and arriving-today packages for the window.

    Args:
        since: Start of the collection window (inclusive), naive UTC.
        until: End of the collection window (exclusive), naive UTC; its date is "today" for the
            arriving-today rule.

    Returns:
        CollectionResult(source="packages", ...). status is "ok"; this collector reads only the
        local database and lets a broken session surface to the runner.
    """
    items: list[Item] = []
    seen: set[str] = set()
    end_date = until.date()

    with db.get_session() as session:
        packages = session.exec(select(Package).where(Package.archived == False)).all()  # noqa: E712
        for package in packages:
            if package.id is None:
                continue
            occurred_at: Optional[datetime] = None
            if package.status == "delivered" and package.delivered_at and since <= package.delivered_at < until:
                occurred_at = package.delivered_at
            elif package.status == "out_for_delivery" and package.last_event_at and since <= package.last_event_at < until:
                occurred_at = package.last_event_at
            elif package.status not in TERMINAL_STATUSES and package.eta and package.eta.date() == end_date:
                occurred_at = package.eta
            if occurred_at is None:
                continue
            item = _item(package, occurred_at)
            if item.external_id in seen:
                continue
            seen.add(item.external_id)
            items.append(item)

    return CollectionResult("packages", "ok", f"{len(items)} package update(s)", items)
