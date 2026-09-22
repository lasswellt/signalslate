"""
Digest collector for the "domains" source: turns pipeline.domains.snapshot.diff() findings into
digest Items.

Unlike every other collector in this package, this one makes no network call and holds no
credentials — the network work already happened when pipeline.domains.inventory.refresh_snapshots()
captured each DomainSnapshot row (a separate scheduled job; see pipeline/scheduler.py). This
collector only reads what is already in the database and diffs it, which is why check_domains()
in pipeline.health can be a plain row count with no external dependency to fail.

Two passes per domain, matching the digest window convention used by every other source
([since, until)):

1. For each DomainSnapshot taken in [since, until), diff it against the domain's immediately
   preceding snapshot (which may itself be older than `since`, or absent for a domain's first
   snapshot ever). This captures ns/mx/a/lock/mail-posture deltas plus whatever current-state
   findings (expiry_soon, auto_renew_off, redemption) apply to that snapshot.
2. A second, prev=None diff() against the domain's latest snapshot as of `until` (which may be the
   same snapshot pass 1 already used, or an older one if no new snapshot landed this window).
   Needed because expiry_soon is a function of `now`, not of new data: a domain can newly cross the
   30-day threshold with no DNS/RDAP change at all, and pass 1 alone would never notice since it
   only looks at snapshots taken inside the window.

Both passes key external_id off the triggering snapshot's row id (`f"{name}:{kind}:{snapshot_id}"`),
so the two passes' overlap on an in-window latest snapshot produces identical duplicate Items —
harmless, since _persist() dedupes on external_id both within one CollectionResult and against
rows already stored for this source. That id also makes the alert dedupe run-to-run: it keeps
firing every run against the same unchanged snapshot until a new snapshot (a new id) is captured,
by design — existing_external_ids() then already has it stored.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from sqlmodel import col, select

from pipeline import db
from pipeline.collectors import CollectionResult, Item
from pipeline.db import Domain, DomainSnapshot
from pipeline.domains.snapshot import diff

_log = logging.getLogger(__name__)


def _load(raw: str, domain_name: str, snapshot_id: Optional[int], failures: list[str]) -> Optional[dict]:
    """Decode a stored snapshot's JSON, recording a failure instead of raising on corrupt data."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        _log.warning(
            "domains collector: bad snapshot json for %s (id=%s): %s", domain_name, snapshot_id, type(exc).__name__
        )
        failures.append(f"{domain_name}: {type(exc).__name__}")
        return None


def _changes_to_items(name: str, changes: list, snapshot_id: int, occurred_at: datetime) -> list[Item]:
    return [
        Item(
            item_type="domain_alert",
            external_id=f"{name}:{change.kind}:{snapshot_id}",
            occurred_at=occurred_at,
            payload={"name": name, "kind": change.kind, "summary": change.summary},
        )
        for change in changes
    ]


def collect_domains(since: datetime, until: datetime) -> CollectionResult:
    """
    Diffs every tracked domain's snapshot history for alert-worthy changes in [since, until), plus
    an expiry_soon (and other current-state) check against each domain's latest known snapshot as
    of `until`. No network, no credentials — see module docstring.

    Args:
        since: Start of the collection window (inclusive), naive UTC.
        until: End of the collection window (exclusive for the snapshot scan; the instant
            expiry_soon is measured against), naive UTC.

    Returns:
        CollectionResult(source="domains", ...). status is "partial" when any stored snapshot's
        JSON could not be decoded, else "ok" — this collector never returns "error" since a single
        domain's bad data must not cost every other domain's alerts.
    """
    items: list[Item] = []
    failures: list[str] = []
    domain_count = 0

    with db.get_session() as session:
        domains = session.exec(select(Domain)).all()
        for domain in domains:
            if domain.id is None:
                continue
            domain_count += 1
            snapshots = session.exec(
                select(DomainSnapshot)
                .where(DomainSnapshot.domain_id == domain.id)
                .order_by(col(DomainSnapshot.taken_at))
            ).all()
            if not snapshots:
                continue

            prev_data: Optional[dict] = None
            in_window: list[DomainSnapshot] = []
            for snap in snapshots:
                if snap.taken_at < since:
                    prev_data = _load(snap.data, domain.name, snap.id, failures)
                elif snap.taken_at < until:
                    in_window.append(snap)

            for snap in in_window:
                cur_data = _load(snap.data, domain.name, snap.id, failures)
                if cur_data is None:
                    continue
                changes = diff(prev_data, cur_data, domain, now=until)
                assert snap.id is not None  # snap came from a select(), so it always has a primary key
                items.extend(_changes_to_items(domain.name, changes, snap.id, snap.taken_at))
                prev_data = cur_data

            latest = next((s for s in reversed(snapshots) if s.taken_at <= until), None)
            if latest is not None:
                latest_data = _load(latest.data, domain.name, latest.id, failures)
                if latest_data is not None:
                    assert latest.id is not None
                    changes = diff(None, latest_data, domain, now=until)
                    items.extend(_changes_to_items(domain.name, changes, latest.id, until))

    detail = f"{domain_count} domain(s), {len(items)} alert(s)"
    if failures:
        return CollectionResult("domains", "partial", f"{detail} ({len(failures)} snapshot(s) unreadable: {failures[0]})", items)
    return CollectionResult("domains", "ok", detail, items)
