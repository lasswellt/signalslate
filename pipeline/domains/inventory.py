"""
Domain portfolio sync: registrar adapter factory, sync_all() (registrar inventory -> Domain rows),
add_manual/remove/import_csv (domains with no registrar connection) and refresh_snapshots()
(DomainSnapshot capture via pipeline.domains.snapshot.inspect()).

Design decisions (Research Decision Phase A/B, docs/plans/domain-collector/plan.md):

- registrar_for()'s per-kind imports are local, mirroring pipeline.collectors.dispatch(): importing
  this module (which sync_all/api routes need just to get a per-connection status list) must not
  drag in every adapter's HTTP dependencies (requests, defusedxml) for a caller that never touches
  a particular registrar.
- sync_all() iterates connections in isolation: one connection's list_domains() raising is caught,
  logged by exception CLASS only (never the message — pipeline/redact.py's reasoning: a registrar
  error can echo submitted credentials) and recorded as that connection's SyncStatus, while every
  other connection still runs. registrar_factory is injectable so tests exercise the upsert/
  missing_since logic against a fake Registrar rather than a real adapter/network.
- Domain never gets a hard delete from a sync: a row no longer in a connection's list_domains()
  gets missing_since set (first time only — a second consecutive absence leaves the original
  timestamp), matching pipeline/db.py's Domain docstring ("a registrar drop should read as
  history, not disappear"). A row that reappears has missing_since cleared. remove() extends the
  same invariant to an explicit user removal: it also only sets missing_since, never issues a SQL
  DELETE, so a manually "removed" domain still has full history if re-added later.
- add_manual()/import_csv() never repoint a registrar-sourced row's source/connection_id to
  "manual" — they only ever touch ownership, last_seen and missing_since — so a sync_all() run
  right after a manual edit does not fight it for which registrar owns the row. Ownership itself is
  set only on first creation and left alone on an update, so a domain manually marked "watched"
  keeps that flag if a later registrar sync (or a re-run of add_manual with a different value passed
  by mistake) touches the same row from another path... actually add_manual DOES update ownership
  on every call, since that is the one field it exists to set; only sync_all leaves ownership alone.
- refresh_snapshots() stores a new DomainSnapshot only when data_hash differs from the domain's
  latest stored snapshot, OR (regardless of hash) when the latest stored snapshot is more than a
  day old, so a domain whose public state never changes still gets one heartbeat row per day for
  history instead of a single snapshot forever. inspect_fn is keyword-injectable, matching
  snapshot.inspect()'s own injectable lookups: tests supply a fake here instead of mocking
  pipeline.domains.snapshot or pipeline.domains.dns/rdap.
"""
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import col, select

from pipeline import connections, db
from pipeline.clock import utcnow
from pipeline.connections import ConnectionView
from pipeline.db import Domain, DomainSnapshot
from pipeline.domains import normalize_domain
from pipeline.domains import snapshot as domain_snapshot
from pipeline.domains.registrars import Registrar, RegistrarDomain, RegistrarError

_log = logging.getLogger(__name__)

# The connections.KINDS entries that are registrar adapters, not collector sources (m365/zoom/
# slack/gmail share the same connection table but have no Registrar adapter).
_REGISTRAR_KINDS = frozenset({"namecheap", "godaddy", "wordpress"})
_OWNERSHIP_VALUES = ("owned", "watched")
_MAX_CSV_FIELDS = 2
# A stored snapshot older than this still gets a fresh row even when nothing changed, so a stable
# domain has at least one heartbeat snapshot per calendar day for history.
_HEARTBEAT_INTERVAL = timedelta(days=1)


class UnknownRegistrar(RegistrarError):
    """connection_id is not a live connection, or its kind has no Registrar adapter."""


@dataclass(frozen=True)
class SyncStatus:
    """One registrar connection's sync_all() outcome."""
    connection_id: str
    kind: str
    status: str  # "ok" | "error"
    detail: str  # human-readable outcome; never a raw registrar response (RegistrarError contract)
    domain_count: int = 0  # domains listed this pass; 0 on error


@dataclass(frozen=True)
class SnapshotStatus:
    """One domain's refresh_snapshots() outcome."""
    name: str
    status: str  # "stored" | "unchanged" | "error"
    detail: str


@dataclass(frozen=True)
class ImportResult:
    """import_csv()'s outcome: names actually added/updated, and every rejected row."""
    added: list[str] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (1-based line number, reason)


def registrar_for(connection_id: str) -> Registrar:
    """
    Builds the registrar adapter for a stored connection, dispatching on its kind.

    Args:
        connection_id: A stored connection id.

    Returns:
        A live Registrar (NamecheapClient | GoDaddyClient | WordPressClient) for connection_id.

    Raises:
        UnknownRegistrar: no such connection, or its kind has no registrar adapter.
        AuthFailed: the connection exists but is missing required config/secrets (raised by the
            adapter's own constructor; see pipeline/domains/registrars/<kind>.py).
    """
    view = connections.get(connection_id)
    if view is None or view.kind not in _REGISTRAR_KINDS:
        raise UnknownRegistrar(f"connection {connection_id!r} has no registrar adapter")
    if view.kind == "namecheap":
        from pipeline.domains.registrars.namecheap import NamecheapClient
        return NamecheapClient(connection_id)
    if view.kind == "godaddy":
        from pipeline.domains.registrars.godaddy import GoDaddyClient
        return GoDaddyClient(connection_id)
    from pipeline.domains.registrars.wordpress import WordPressClient
    return WordPressClient(connection_id)


def _upsert_from_registrar(session: "db.Session", view: ConnectionView, item: RegistrarDomain, now: datetime) -> None:
    """Creates or updates one Domain row from one registrar-reported entry. Only sets ownership on
    create; an existing row keeps whatever ownership it already has."""
    row = session.exec(select(Domain).where(Domain.name == item.name)).first()
    if row is None:
        row = Domain(name=item.name, ownership="owned", source=view.kind, connection_id=view.id, first_seen=now)
    row.source = view.kind
    row.connection_id = view.id
    row.expires_at = item.expires_at
    row.auto_renew = item.auto_renew
    row.locked = item.locked
    row.privacy = item.privacy
    row.last_seen = now
    row.missing_since = None
    session.add(row)


def _sync_one(view: ConnectionView, registrar_factory: Callable[[str], Registrar]) -> SyncStatus:
    try:
        registrar = registrar_factory(view.id)
        listed = registrar.list_domains()
    except Exception as exc:  # one connection's failure must never stop the others
        _log.warning("domains sync: connection %s failed: %s", view.id, type(exc).__name__)
        return SyncStatus(connection_id=view.id, kind=view.kind, status="error", detail=type(exc).__name__)

    now = utcnow()
    seen_names = {item.name for item in listed}
    with db.get_session() as session:
        for item in listed:
            _upsert_from_registrar(session, view, item, now)
        # Rows previously sourced from this connection but absent from this pass are history, not
        # deleted: mark missing_since (first time only) rather than removing the row.
        tracked = session.exec(select(Domain).where(Domain.connection_id == view.id)).all()
        for row in tracked:
            if row.name in seen_names or row.missing_since is not None:
                continue
            row.missing_since = now
            session.add(row)
        session.commit()
    return SyncStatus(connection_id=view.id, kind=view.kind, status="ok", detail=f"synced {len(listed)} domain(s)", domain_count=len(listed))


def sync_all(*, registrar_factory: Callable[[str], Registrar] = registrar_for) -> list[SyncStatus]:
    """
    Syncs every registrar connection's domain list into the Domain table.

    Args:
        registrar_factory: Builds a Registrar from a connection id. Defaults to registrar_for;
            tests inject a fake to exercise the upsert/missing_since logic without a real adapter.

    Returns:
        One SyncStatus per registrar connection (namecheap/godaddy/wordpress), in connection
        creation order. A connection of a non-registrar kind, or one switched off on the Accounts page,
        is skipped entirely (not included).
    """
    from pipeline import config_store  # local import: config_store reaches the connection store

    switched = config_store.load_config().get("connection_active", {})
    return [
        _sync_one(view, registrar_factory)
        for view in connections.list_connections()
        if view.kind in _REGISTRAR_KINDS and switched.get(view.id, True)
    ]


def add_manual(name: str, ownership: str = "owned") -> Domain:
    """
    Adds or updates one domain that has no registrar connection: pure ownership tracking, for a
    domain bought elsewhere ("owned") or a competitor/interesting name with no purchase intent
    ("watched"). Never repoints a registrar-sourced row's source/connection_id — only ownership,
    last_seen and missing_since are touched, so a later sync_all() still owns that row's registrar
    fields.

    Args:
        name: Domain as typed by the user; any form normalize_domain() accepts.
        ownership: "owned" or "watched".

    Returns:
        The stored Domain row (detached from its session; every field is already loaded).

    Raises:
        ValueError: name fails normalize_domain(), or ownership is not "owned"/"watched".
    """
    if ownership not in _OWNERSHIP_VALUES:
        raise ValueError(f"ownership must be one of {_OWNERSHIP_VALUES}: {ownership!r}")
    normalized, _tld = normalize_domain(name)
    now = utcnow()
    with db.get_session() as session:
        row = session.exec(select(Domain).where(Domain.name == normalized)).first()
        if row is None:
            row = Domain(name=normalized, ownership=ownership, source="manual", first_seen=now)
        else:
            row.ownership = ownership
        row.last_seen = now
        row.missing_since = None
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def remove(name: str) -> bool:
    """
    Marks a domain as no longer tracked. Like a vanished registrar row, this sets missing_since
    rather than issuing a DELETE (pipeline/db.py's Domain docstring: every row is updated in place,
    never dropped, so history and a later re-add both still work).

    Args:
        name: Domain name, any form normalize_domain() accepts.

    Returns:
        True when a live (missing_since is None) row was found and marked, False when there was no
        such row or it was already marked missing.

    Raises:
        ValueError: name fails normalize_domain().
    """
    normalized, _tld = normalize_domain(name)
    now = utcnow()
    with db.get_session() as session:
        row = session.exec(select(Domain).where(Domain.name == normalized)).first()
        if row is None or row.missing_since is not None:
            return False
        row.missing_since = now
        session.add(row)
        session.commit()
        return True


def import_csv(text: str) -> ImportResult:
    """
    Bulk add_manual() from CSV/plain text: one domain per line, "domain" or "domain,ownership"
    ("owned" when omitted). Blank lines and lines starting with "#" are skipped. Every row is
    independent, so one bad line (unparseable domain, bad ownership value, extra fields) is
    rejected and reported while the rest of the import still runs.

    Args:
        text: The raw CSV/plain-text body.

    Returns:
        ImportResult: normalized names actually added/updated, and (1-based line_number, reason)
        for every rejected row.
    """
    result = ImportResult()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) > _MAX_CSV_FIELDS:
            result.rejected.append((line_number, "too many fields"))
            continue
        name = parts[0]
        ownership = parts[1] if len(parts) > 1 and parts[1] else "owned"
        if ownership not in _OWNERSHIP_VALUES:
            result.rejected.append((line_number, f"invalid ownership {ownership!r}"))
            continue
        try:
            row = add_manual(name, ownership)
        except ValueError as exc:
            result.rejected.append((line_number, str(exc)))
            continue
        result.added.append(row.name)
    return result


def _refresh_one(session: "db.Session", row: Domain, inspect_fn: Callable[[str], dict], now: datetime) -> SnapshotStatus:
    try:
        data = inspect_fn(row.name)
    except Exception as exc:  # inspect_fn itself never raises for an expected outcome; guards a misbehaving injected fake
        _log.warning("domains refresh: inspect failed for %s: %s", row.name, type(exc).__name__)
        return SnapshotStatus(name=row.name, status="error", detail=type(exc).__name__)

    data_hash = str(data.get("data_hash") or "")
    assert row.id is not None  # row came from a select() on the DB, so it always has a primary key
    latest = session.exec(
        select(DomainSnapshot).where(DomainSnapshot.domain_id == row.id).order_by(col(DomainSnapshot.taken_at).desc())
    ).first()

    if latest is not None and latest.data_hash == data_hash and (now - latest.taken_at) < _HEARTBEAT_INTERVAL:
        return SnapshotStatus(name=row.name, status="unchanged", detail="no change since last snapshot")

    session.add(DomainSnapshot(domain_id=row.id, taken_at=now, data=json.dumps(data, sort_keys=True), data_hash=data_hash))
    return SnapshotStatus(name=row.name, status="stored", detail="snapshot stored")


def refresh_snapshots(
    *,
    inspect_fn: Callable[[str], dict] = domain_snapshot.inspect,
    now: Optional[datetime] = None,
) -> list[SnapshotStatus]:
    """
    Captures one inspect() snapshot per tracked (non-missing) Domain row, storing a new
    DomainSnapshot only when its data_hash differs from that domain's latest stored snapshot, or
    when the latest stored snapshot is more than a day old (see _HEARTBEAT_INTERVAL) regardless of
    hash, so a domain with a stable posture still gets one heartbeat row per day.

    A domain currently marked missing (missing_since is not None) is skipped: there is nothing
    useful to inspect for a name no longer in the portfolio.

    Args:
        inspect_fn: A pipeline.domains.snapshot.inspect-like callable, keyed only by name. Defaults
            to the real inspect() (live DNS/RDAP lookups); tests inject a fake.
        now: The instant snapshots are taken at / the heartbeat age is measured against. Defaults
            to pipeline.clock.utcnow().

    Returns:
        One SnapshotStatus per live (non-missing) domain, in no particular guaranteed order.
    """
    clock_now = now if now is not None else utcnow()
    results: list[SnapshotStatus] = []
    with db.get_session() as session:
        rows = [row for row in session.exec(select(Domain)).all() if row.missing_since is None]
        for row in rows:
            results.append(_refresh_one(session, row, inspect_fn, clock_now))
        session.commit()
    return results
