"""
Domain portfolio routes (Research Finding 8, docs/plans/domain-collector/plan.md/spec.md).

Design decisions:

- Reads (list/detail) query pipeline.db.Domain/DomainSnapshot directly with sqlmodel select, the
  same pattern api.routers.connections uses for db.Tombstone: pipeline.domains.inventory exposes no
  list function of its own (sync_all/add_manual/remove/import_csv/refresh_snapshots are all
  mutations), so there is nothing to call through for a plain filtered read.
- sync_all() and refresh_snapshots() are blocking network/DB calls, so every handler here is plain
  `def` (connections.py router pattern) and runs in FastAPI's threadpool.
- POST /domains/inspect never writes a DomainSnapshot row: it calls snapshot.inspect() (and, when
  intel=true, intel.subdomains()/intel.archived_urls()) directly rather than going through
  inventory.refresh_snapshots(), which is the only path that persists.
- DELETE only ever sets missing_since (via inventory.remove(), never a SQL DELETE) and is refused
  for a registrar-sourced "owned" row: that row is still actively synced, so removing it via this
  endpoint would just have it reappear (missing_since cleared) on the next POST /domains/sync. A
  manual row, or any row a user has marked "watched", has no such sync putting it back.
- Errors are {code, message} (api.routers.connections._coded pattern) and never echo a registrar
  response body or raw exception text — only str(ValueError) from normalize_domain/add_manual,
  which is already a fixed, credential-free message.
- Every module-level pipeline.domains function is imported by module (not by name) so tests can
  monkeypatch it at this file's own import site (inventory.sync_all, domain_snapshot.inspect, ...)
  instead of touching pipeline.domains.inventory/snapshot/intel or the network.
"""
import json
import logging
from dataclasses import asdict
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from api.serialize import iso_z
from pipeline import db
from pipeline.db import Domain, DomainSnapshot
from pipeline.domains import inventory, normalize_domain
from pipeline.domains import intel as domain_intel
from pipeline.domains import snapshot as domain_snapshot
from pipeline.domains.registrars._egress import current_egress_ip

router = APIRouter(tags=["domains"])

_log = logging.getLogger(__name__)

# A pasted CSV import body is capped well above any real portfolio (one line per domain) but far
# short of a request that could tie up the worker thread parsing it.
_MAX_IMPORT_CHARS = 200_000
_MAX_SNAPSHOT_HISTORY = 30


class DomainOut(BaseModel):
    name: str
    ownership: str
    source: str
    connection_id: Optional[str]
    expires_at: Optional[str]
    auto_renew: Optional[bool]
    locked: Optional[bool]
    privacy: Optional[bool]
    first_seen: Optional[str]
    last_seen: Optional[str]
    missing_since: Optional[str]


class SnapshotSummary(BaseModel):
    taken_at: Optional[str]
    data_hash: str


class DomainDetailOut(DomainOut):
    latest: Optional[dict[str, Any]] = None
    latest_taken_at: Optional[str] = None
    history: list[SnapshotSummary] = []


class AddBody(BaseModel):
    """Rejects unknown keys outright: unlike connections.py's secret-bearing bodies, nothing here
    could leak a credential into a 422, so there is no need to hide the field name."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ownership: Literal["owned", "watched"] = "owned"


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(max_length=_MAX_IMPORT_CHARS)


class RejectedRow(BaseModel):
    line: int
    reason: str


class ImportOut(BaseModel):
    added: list[str]
    rejected: list[RejectedRow]


class EgressIpOut(BaseModel):
    # "unknown" (never null) on lookup failure, matching current_egress_ip()'s own contract; the
    # connection form treats it as "could not detect" and leaves the field for the user to fill in.
    ip: str


class SyncStatusOut(BaseModel):
    connection_id: str
    kind: str
    status: str
    detail: str
    domain_count: int


class SnapshotStatusOut(BaseModel):
    name: str
    status: str
    detail: str


class SyncOut(BaseModel):
    sync: list[SyncStatusOut]
    refresh: list[SnapshotStatusOut]


class InspectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    intel: bool = False


class InspectOut(BaseModel):
    name: str
    dns: dict[str, Any]
    mail: dict[str, Any]
    rdap: dict[str, Any]
    data_hash: str
    intel: Optional[dict[str, Any]] = None


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _domain_fields(row: Domain) -> dict[str, Any]:
    return {
        "name": row.name,
        "ownership": row.ownership,
        "source": row.source,
        "connection_id": row.connection_id,
        "expires_at": iso_z(row.expires_at),
        "auto_renew": row.auto_renew,
        "locked": row.locked,
        "privacy": row.privacy,
        "first_seen": iso_z(row.first_seen),
        "last_seen": iso_z(row.last_seen),
        "missing_since": iso_z(row.missing_since),
    }


def _domain_out(row: Domain) -> DomainOut:
    return DomainOut(**_domain_fields(row))


def _get_domain(normalized: str) -> Domain:
    with db.get_session() as session:
        row = session.exec(select(Domain).where(Domain.name == normalized)).first()
        if row is None:
            raise _coded(404, "domain_not_found", "Domain not found")
        session.expunge(row)
        return row


@router.get("/domains", response_model=list[DomainOut])
def list_domains(ownership: Optional[str] = None, source: Optional[str] = None) -> list[DomainOut]:
    """Every tracked domain (including one marked missing_since), optionally filtered by ownership
    ("owned"|"watched") and/or source ("manual"|"namecheap"|"godaddy"|"wordpress")."""
    with db.get_session() as session:
        statement = select(Domain)
        if ownership is not None:
            statement = statement.where(Domain.ownership == ownership)
        if source is not None:
            statement = statement.where(Domain.source == source)
        rows = session.exec(statement.order_by(col(Domain.name))).all()
    return [_domain_out(row) for row in rows]


@router.get("/domains/egress-ip", response_model=EgressIpOut)
def get_egress_ip() -> EgressIpOut:
    """
    This server's current public IPv4, for prefilling a Namecheap connection's client_ip field —
    Namecheap gates its API by an IP allowlist the caller configures on both sides (this field, and
    Namecheap's own whitelist panel), and that address is not something Namecheap hands out, so the
    UI offers to detect it instead of leaving the user to find it themselves.

    Registered ahead of GET /domains/{name} (api/main.py's router order already matters here, see
    its own comment): a route named exactly "egress-ip" would otherwise be looked up as a domain
    name by that catch-all — the same class of bug the router-order fix a few commits back exists
    to prevent, so this one is placed correctly from the start rather than by luck.
    """
    return EgressIpOut(ip=current_egress_ip())


@router.get("/domains/{name}", response_model=DomainDetailOut)
def get_domain(name: str) -> DomainDetailOut:
    """The domain's stored fields plus its latest snapshot (full data) and up to the last 30
    snapshots summarized (taken_at, data_hash)."""
    try:
        normalized, _tld = normalize_domain(name)
    except ValueError:
        raise _coded(404, "domain_not_found", "Domain not found") from None
    with db.get_session() as session:
        row = session.exec(select(Domain).where(Domain.name == normalized)).first()
        if row is None:
            raise _coded(404, "domain_not_found", "Domain not found")
        assert row.id is not None  # row came from a select() on the DB, so it always has a primary key
        snapshots = session.exec(
            select(DomainSnapshot)
            .where(DomainSnapshot.domain_id == row.id)
            .order_by(col(DomainSnapshot.taken_at).desc())
            .limit(_MAX_SNAPSHOT_HISTORY)
        ).all()
        fields = _domain_fields(row)
    latest = snapshots[0] if snapshots else None
    return DomainDetailOut(
        **fields,
        latest=json.loads(latest.data) if latest is not None else None,
        latest_taken_at=iso_z(latest.taken_at) if latest is not None else None,
        history=[SnapshotSummary(taken_at=iso_z(s.taken_at), data_hash=s.data_hash) for s in snapshots],
    )


@router.post("/domains", status_code=201, response_model=DomainOut)
def add_domain(body: AddBody) -> DomainOut:
    """Manually adds/updates a domain with no registrar connection."""
    try:
        row = inventory.add_manual(body.name, body.ownership)
    except ValueError as exc:
        raise _coded(422, "invalid_domain", str(exc)) from None
    return _domain_out(row)


@router.delete("/domains/{name}", status_code=204)
def delete_domain(name: str) -> Response:
    """
    Marks a domain as no longer tracked (missing_since, never a SQL DELETE). Refused for a
    registrar-sourced "owned" row: it would just reappear on the next POST /domains/sync.
    """
    try:
        normalized, _tld = normalize_domain(name)
    except ValueError:
        raise _coded(404, "domain_not_found", "Domain not found") from None
    row = _get_domain(normalized)
    if row.missing_since is not None:
        raise _coded(404, "domain_not_found", "Domain not found")
    if row.source != "manual" and row.ownership != "watched":
        raise _coded(
            409,
            "registrar_owned",
            "This domain is owned via a registrar sync; remove the connection or watch it instead",
        )
    inventory.remove(normalized)
    return Response(status_code=204)


@router.post("/domains/import", response_model=ImportOut)
def import_domains(body: ImportBody) -> ImportOut:
    """Bulk add_manual() from CSV/plain text, one domain (or "domain,ownership") per line."""
    result = inventory.import_csv(body.csv)
    return ImportOut(
        added=result.added,
        rejected=[RejectedRow(line=line_number, reason=reason) for line_number, reason in result.rejected],
    )


@router.post("/domains/sync", response_model=SyncOut)
def sync_domains() -> SyncOut:
    """Syncs every registrar connection's domain list, then refreshes DNS/RDAP/mail snapshots for
    every live (non-missing) domain."""
    sync_statuses = inventory.sync_all()
    refresh_statuses = inventory.refresh_snapshots()
    return SyncOut(
        sync=[SyncStatusOut(**asdict(status)) for status in sync_statuses],
        refresh=[SnapshotStatusOut(**asdict(status)) for status in refresh_statuses],
    )


@router.post("/domains/inspect", response_model=InspectOut)
def inspect_domain(body: InspectBody) -> InspectOut:
    """
    Inspects any domain (in the portfolio or not) on demand, without persisting a DomainSnapshot.
    When intel=true, also runs the best-effort crt.sh subdomain and Wayback archived-URL lookups.
    """
    try:
        normalized, _tld = normalize_domain(body.name)
    except ValueError as exc:
        raise _coded(422, "invalid_domain", str(exc)) from None

    data = domain_snapshot.inspect(normalized)
    intel_out: Optional[dict[str, Any]] = None
    if body.intel:
        subdomains_result = domain_intel.subdomains(normalized)
        archived_result = domain_intel.archived_urls(normalized)
        intel_out = {
            "subdomains": {
                "status": subdomains_result.status,
                "names": list(subdomains_result.names),
                "error": subdomains_result.error,
            },
            "archived_urls": {
                "status": archived_result.status,
                "urls": list(archived_result.urls),
                "error": archived_result.error,
            },
        }
    return InspectOut(
        name=normalized,
        dns=data["dns"],
        mail=data["mail"],
        rdap=data["rdap"],
        data_hash=data["data_hash"],
        intel=intel_out,
    )
