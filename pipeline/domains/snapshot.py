"""
Assembles DNS, RDAP and mail-posture lookups into one point-in-time domain snapshot, and diffs two
snapshots into digest-worthy Change events (Research Finding 5).

Design decisions:

- inspect()'s three lookups (lookup_records, mail_posture, lookup_rdap) are keyword-injectable,
  defaulting to the real pipeline.domains.dns/rdap functions. This matches the subsystem's existing
  test-injection pattern (dns.py's resolver param, rdap.py's domain_lookup param): tests supply
  fakes here instead of mocking pipeline.domains.dns/rdap.
- Each of the three parts is wrapped independently: a raised exception from one injected lookup
  becomes {"status": "error", "error": <exception class name>} for that part only, never failing
  the whole snapshot (research doc: "Each part records ok|error|unsupported rather than failing
  the whole snapshot"). rdap's own ok|not_found|unsupported|error status passes through unchanged;
  dns/mail, which never raise for an expected outcome, get "ok" unless the call itself blows up.
- The assembled dict is the one JSON-serializable value used both as DomainSnapshot.data and as the
  hash input: datetimes become iso_z() strings (dataclasses.asdict() leaves them as datetime
  objects, which json.dumps cannot handle) so the same dict round-trips through json.dumps/loads
  without a custom encoder anywhere else in the codebase.
- data_hash is sha256 over json.dumps(..., sort_keys=True) of the assembled fields (excluding the
  hash key itself, which cannot include itself): key ordering or an equivalent-but-differently-
  ordered dict never produces a spurious hash difference, matching DomainSnapshot's "cheap
  unchanged-snapshot detection" role (pipeline/db.py).
- diff() is pure: prev/cur are already-decoded snapshot dicts (prev is None for a domain's first
  inspection) and domain is the pipeline.db.Domain row for auto_renew/ownership/expiry context —
  diff() does no DB or network I/O of its own. now is injectable (defaults to pipeline.clock.utcnow)
  so expiry_soon is testable without patching the clock.
- ns_changed/mx_changed/a_changed/lock_removed/mail_posture_regressed are true prev-vs-cur deltas
  and need a prior snapshot to fire. expiry_soon/auto_renew_off/redemption are current-state checks
  using the Domain row as context (auto_renew, ownership) and fire on cur alone, including a
  domain's very first inspection, since they are digest-worthy on their own regardless of whether
  anything changed since the last run.
"""
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from api.serialize import iso_z
from pipeline.clock import utcnow
from pipeline.db import Domain
from pipeline.domains import dns as domain_dns
from pipeline.domains import rdap as domain_rdap

_log = logging.getLogger(__name__)

# RDAP status values (RFC 8056 EPP camelCase, per pipeline.domains.rdap._camel_case_status) that
# mean a watched domain may be about to free up.
_REDEMPTION_STATUSES = frozenset({"redemptionPeriod", "pendingDelete"})

_EXPIRY_URGENT_DAYS = 7
_EXPIRY_WARNING_DAYS = 30


@dataclass(frozen=True)
class Change:
    """One digest-worthy finding from diff(). kind is a stable machine key (see module docstring
    for the full list); summary is the human-readable digest line."""
    kind: str
    summary: str


def _json_safe(value: Any) -> Any:
    """Recursively converts a dataclasses.asdict() result to a JSON-serializable value: the only
    non-serializable type these dataclasses carry is datetime (RdapResult.created/expires)."""
    if isinstance(value, datetime):
        return iso_z(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _dns_part(name: str, lookup_records: Callable[[str], dict]) -> dict:
    try:
        records = lookup_records(name)
    except Exception as exc:  # an injected lookup misbehaving, not an expected DNS outcome
        _log.warning("domain snapshot: dns lookup failed for %s: %s", name, type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__, "records": {}}
    return {
        "status": "ok",
        "error": None,
        "records": {rtype: _json_safe(asdict(result)) for rtype, result in records.items()},
    }


def _mail_part(name: str, mail_posture: Callable[[str], Any]) -> dict:
    try:
        posture = mail_posture(name)
    except Exception as exc:
        _log.warning("domain snapshot: mail posture lookup failed for %s: %s", name, type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__}
    posture_dict = _json_safe(asdict(posture))
    posture_dict["status"] = "ok"
    posture_dict["error"] = None
    return posture_dict


def _rdap_part(name: str, lookup_rdap: Callable[[str], Any]) -> dict:
    try:
        result = lookup_rdap(name)
    except Exception as exc:  # lookup_rdap itself never raises; guards a misbehaving injected fake
        _log.warning("domain snapshot: rdap lookup failed for %s: %s", name, type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__}
    return _json_safe(asdict(result))


def _data_hash(fields: dict) -> str:
    """sha256 over the assembled fields, sorted so key order never changes the hash."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def inspect(
    name: str,
    *,
    lookup_records: Callable[[str], dict] = domain_dns.lookup_records,
    mail_posture: Callable[[str], Any] = domain_dns.mail_posture,
    lookup_rdap: Callable[[str], Any] = domain_rdap.lookup_rdap,
) -> dict:
    """
    Assembles one point-in-time snapshot of name's public state: DNS records, mail-authentication
    posture and RDAP registration data.

    Args:
        name: A domain as normalize_domain() returns it (lower-case punycode).
        lookup_records: A pipeline.domains.dns.lookup_records-like callable. Defaults to the real
            function; tests inject a fake instead of hitting the network.
        mail_posture: A pipeline.domains.dns.mail_posture-like callable. Defaults to the real
            function; tests inject a fake.
        lookup_rdap: A pipeline.domains.rdap.lookup_rdap-like callable. Defaults to the real
            function; tests inject a fake.

    Returns:
        A JSON-serializable dict with "dns", "mail" and "rdap" sections (each carrying its own
        "status": "ok"|"error"|"unsupported"|"not_found") plus "data_hash", a sha256 over the three
        sections' normalized (sorted-key) JSON. Never raises: a failing lookup becomes that
        section's status="error" rather than failing the whole snapshot.
    """
    fields = {
        "dns": _dns_part(name, lookup_records),
        "mail": _mail_part(name, mail_posture),
        "rdap": _rdap_part(name, lookup_rdap),
    }
    return {**fields, "data_hash": _data_hash(fields)}


def _record_values(dns_section: dict, rtype: str) -> Optional[set]:
    """The record type's value set from a snapshot's "dns" section, or None when that section
    itself failed (status != "ok") and the set cannot be trusted."""
    if dns_section.get("status") != "ok":
        return None
    record = (dns_section.get("records") or {}).get(rtype) or {}
    if not record.get("ok", False):
        return None
    return set(record.get("values") or ())


def _record_set_change(prev_dns: dict, cur_dns: dict, rtype: str, kind: str) -> Optional[Change]:
    prev_values = _record_values(prev_dns, rtype)
    cur_values = _record_values(cur_dns, rtype)
    if prev_values is None or cur_values is None or prev_values == cur_values:
        return None
    return Change(kind=kind, summary=f"{rtype} records changed: {sorted(prev_values)} -> {sorted(cur_values)}")


def _lock_removed(prev_rdap: dict, cur_rdap: dict) -> Optional[Change]:
    if prev_rdap.get("status") != "ok" or cur_rdap.get("status") != "ok":
        return None
    if prev_rdap.get("locked") is True and cur_rdap.get("locked") is False:
        return Change(kind="lock_removed", summary="registrar transfer lock was removed")
    return None


def _mail_regressed(prev_mail: dict, cur_mail: dict) -> Optional[Change]:
    if prev_mail.get("status") != "ok" or cur_mail.get("status") != "ok":
        return None
    prev_flags = set(prev_mail.get("flags") or ())
    cur_flags = set(cur_mail.get("flags") or ())
    new_flags = cur_flags - prev_flags
    if new_flags:
        return Change(kind="mail_posture_regressed", summary=f"mail posture regressed: {sorted(new_flags)}")
    prev_spf = prev_mail.get("spf") or {}
    cur_spf = cur_mail.get("spf") or {}
    if prev_spf.get("present") and not cur_spf.get("present"):
        return Change(kind="mail_posture_regressed", summary="SPF record was removed")
    prev_dmarc = prev_mail.get("dmarc") or {}
    cur_dmarc = cur_mail.get("dmarc") or {}
    if prev_dmarc.get("policy") == "reject" and cur_dmarc.get("policy") != "reject":
        return Change(
            kind="mail_posture_regressed",
            summary=f"DMARC policy weakened from reject to {cur_dmarc.get('policy')!r}",
        )
    return None


def _expiry_days_left(cur_rdap: dict, domain: Domain, now: datetime) -> Optional[int]:
    """Days until expiry, preferring this snapshot's RDAP expires date and falling back to the
    Domain row's registrar-reported expires_at when RDAP did not resolve one."""
    expires: Optional[datetime] = None
    if cur_rdap.get("status") == "ok" and cur_rdap.get("expires"):
        expires = datetime.strptime(cur_rdap["expires"], "%Y-%m-%dT%H:%M:%SZ")
    elif domain.expires_at is not None:
        expires = domain.expires_at
    if expires is None:
        return None
    return (expires - now).days


def _expiry_soon(cur_rdap: dict, domain: Domain, now: datetime) -> Optional[Change]:
    if domain.auto_renew is True:
        return None
    days_left = _expiry_days_left(cur_rdap, domain, now)
    if days_left is None or days_left > _EXPIRY_WARNING_DAYS:
        return None
    if days_left <= _EXPIRY_URGENT_DAYS:
        return Change(kind="expiry_soon", summary=f"expires in {days_left} day(s), auto-renew is off")
    return Change(kind="expiry_soon", summary=f"expires in {days_left} day(s)")


def _auto_renew_off(domain: Domain) -> Optional[Change]:
    if domain.auto_renew is False:
        return Change(kind="auto_renew_off", summary="auto-renew is off")
    return None


def _redemption(cur_rdap: dict, domain: Domain) -> Optional[Change]:
    if domain.ownership != "watched" or cur_rdap.get("status") != "ok":
        return None
    statuses = set(cur_rdap.get("statuses") or ())
    hit = statuses & _REDEMPTION_STATUSES
    if not hit:
        return None
    return Change(kind="redemption", summary=f"watched domain entered {sorted(hit)}")


def diff(
    prev: Optional[dict],
    cur: dict,
    domain: Domain,
    *,
    now: Optional[datetime] = None,
) -> list[Change]:
    """
    Compares two inspect() snapshots (prev may be None for a domain's first inspection) and returns
    the digest-worthy Change events.

    Args:
        prev: The domain's previous snapshot dict (inspect()'s return shape), or None.
        cur: The domain's current snapshot dict.
        domain: The pipeline.db.Domain row, for auto_renew/ownership/expiry context.
        now: The instant to measure expiry against; defaults to pipeline.clock.utcnow().

    Returns:
        A list[Change], possibly empty. ns_changed/mx_changed/a_changed/lock_removed/
        mail_posture_regressed only fire when prev is present and the relevant section differs.
        expiry_soon/auto_renew_off/redemption are current-state checks and can fire even when
        prev is None.
    """
    clock_now = now if now is not None else utcnow()
    changes: list[Change] = []

    if prev is not None:
        prev_dns = prev.get("dns") or {}
        cur_dns = cur.get("dns") or {}
        for rtype, kind in (("NS", "ns_changed"), ("MX", "mx_changed"), ("A", "a_changed")):
            change = _record_set_change(prev_dns, cur_dns, rtype, kind)
            if change is not None:
                changes.append(change)

        lock_change = _lock_removed(prev.get("rdap") or {}, cur.get("rdap") or {})
        if lock_change is not None:
            changes.append(lock_change)

        mail_change = _mail_regressed(prev.get("mail") or {}, cur.get("mail") or {})
        if mail_change is not None:
            changes.append(mail_change)

    cur_rdap = cur.get("rdap") or {}

    expiry_change = _expiry_soon(cur_rdap, domain, clock_now)
    if expiry_change is not None:
        changes.append(expiry_change)

    renew_change = _auto_renew_off(domain)
    if renew_change is not None:
        changes.append(renew_change)

    redemption_change = _redemption(cur_rdap, domain)
    if redemption_change is not None:
        changes.append(redemption_change)

    return changes
