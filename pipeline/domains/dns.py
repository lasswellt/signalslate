"""
DNS record lookups and mail-posture analysis for the domains subsystem (Research Finding 5).

Design decisions:

- lookup_records() and mail_posture() return plain typed values (dataclasses), never raise for an
  expected DNS outcome: NXDOMAIN and NoAnswer are data ("this domain/record type has nothing"),
  not a failure. Only a real resolution failure (timeout, SERVFAIL/NoNameservers, a malformed
  response) sets a RecordResult's ok=False, and even then the caller gets the exception CLASS name
  (pipeline.redact / pipeline.runner convention: never log or surface an exception message, which
  can carry resolver-internal detail) rather than a raised exception.
- The resolver is a constructor parameter, not a module-level singleton: pipeline.domains.dns's
  own tests inject a fake object exposing dnspython's Resolver.resolve() signature, so lookups stay
  offline in CI. Production callers get a real dns.resolver.Resolver whose nameservers come from
  domain_settings().resolver (DOMAINS_RESOLVER) when the operator has pointed it at a LAN resolver,
  per the research doc's guidance not to send bulk traffic to a public resolver.
- Both functions fan out their independent per-record-type/per-selector lookups across a bounded
  ThreadPoolExecutor (the codebase's existing sync-requests-plus-thread-pools pattern, matching
  RDAP/CT/registrar collectors elsewhere in this subsystem) rather than going one at a time: a full
  mail_posture() call is 9 DNS queries plus one HTTPS fetch, and serial latency there would make
  the digest refresh job slow for no benefit.
- mail_posture()'s "parked domain" flag reuses only data already fetched (MX, SPF, DMARC): a domain
  with no MX records receives no mail, so RFC-recommended hardening is a domain-wide SPF "-all" and
  DMARC "p=reject" (research doc: "parked domain accepts spoofed mail" is a good digest finding).
- SPF's lookup-count is computed from the top-level record only (include/a/mx/ptr/exists/redirect
  mechanisms), not by recursively resolving each include chain: RFC 7208's 10-lookup ceiling is a
  useful proxy without adding an unbounded recursive-fetch surface to a read-only intel lookup.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import dns.exception
import dns.resolver
import requests

from pipeline.domains import domain_settings

_log = logging.getLogger(__name__)

_RECORD_TYPES: tuple[str, ...] = ("A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA")
DKIM_SELECTORS: tuple[str, ...] = ("google", "selector1", "selector2", "k1", "default")

_MAX_WORKERS = 8
_HTTP_TIMEOUT = 10
_SPF_LOOKUP_LIMIT = 10
_PARKED_FLAG = "parked domain without v=spf1 -all / p=reject"

_QUOTED_SEGMENT = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SPF_LOOKUP_MECH = re.compile(r"(?:^|\s)[+\-~?]?(include:|a(?=[:/\s]|$)|mx(?=[:/\s]|$)|ptr(?=[:/\s]|$)|exists:|redirect=)")
_DMARC_POLICY = re.compile(r"\bp=(\w+)", re.IGNORECASE)
_MTA_STS_MODE = re.compile(r"^\s*mode:\s*(\w+)", re.MULTILINE)


class _ResolverLike(Protocol):
    """What lookup_records()/mail_posture() need from a resolver: dnspython's Resolver.resolve()
    signature. A test double only has to implement this, not subclass dns.resolver.Resolver."""

    def resolve(self, qname: str, rdtype: str, *, raise_on_no_answer: bool = True) -> Any: ...


@dataclass(frozen=True)
class RecordResult:
    """One record-type's lookup outcome. ok=True with empty values covers NXDOMAIN and NoAnswer
    alike: the name or record type legitimately has nothing. ok=False means the lookup itself
    failed (timeout, no reachable nameserver, malformed response); error then holds the exception
    class name, never its message."""
    ok: bool
    values: tuple[str, ...]
    error: Optional[str]


@dataclass(frozen=True)
class SpfResult:
    present: bool
    record: Optional[str]
    lookup_count: Optional[int]
    within_limit: Optional[bool]
    error: Optional[str]


@dataclass(frozen=True)
class DmarcResult:
    present: bool
    record: Optional[str]
    policy: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class DkimResult:
    present: bool
    record: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class MtaStsResult:
    txt_present: bool
    txt_record: Optional[str]
    policy_fetched: bool
    policy_mode: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class BimiResult:
    present: bool
    record: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class MailPosture:
    spf: SpfResult
    dmarc: DmarcResult
    dkim: dict[str, DkimResult]
    mta_sts: MtaStsResult
    bimi: BimiResult
    flags: tuple[str, ...]


def _make_resolver() -> dns.resolver.Resolver:
    """A real dnspython resolver, pointed at DOMAINS_RESOLVER's nameserver(s) when configured."""
    resolver = dns.resolver.Resolver()
    configured = domain_settings().resolver
    if configured:
        resolver.nameservers = [ip.strip() for ip in configured.split(",") if ip.strip()]
    return resolver


def _query_one(resolver: _ResolverLike, qname: str, rdtype: str) -> RecordResult:
    """Runs one resolver.resolve() call, turning NXDOMAIN/NoAnswer into ok=True/empty data and any
    other DNSException into ok=False with the exception class name."""
    try:
        answer = resolver.resolve(qname, rdtype, raise_on_no_answer=True)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return RecordResult(ok=True, values=(), error=None)
    except dns.exception.DNSException as exc:
        _log.warning("dns lookup failed for %s %s: %s", rdtype, qname, type(exc).__name__)
        return RecordResult(ok=False, values=(), error=type(exc).__name__)
    return RecordResult(ok=True, values=tuple(rdata.to_text() for rdata in answer), error=None)


def lookup_records(name: str, resolver: Optional[_ResolverLike] = None) -> dict[str, RecordResult]:
    """
    Looks up A, AAAA, CNAME, MX, NS, TXT, SOA and CAA for name, one RecordResult per type.

    Args:
        name: A domain as normalize_domain() returns it (lower-case punycode).
        resolver: A dnspython-Resolver-like object; defaults to a real resolver built from
            domain_settings(). Tests inject a fake here instead of hitting the network.

    Returns:
        dict keyed by record type ("A", "AAAA", ...), each value a RecordResult. Never raises for
        an expected DNS outcome (NXDOMAIN/NoAnswer); see RecordResult.
    """
    active_resolver = resolver or _make_resolver()
    results: dict[str, RecordResult] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {rdtype: pool.submit(_query_one, active_resolver, name, rdtype) for rdtype in _RECORD_TYPES}
        for rdtype, future in futures.items():
            results[rdtype] = future.result()
    return results


def _unquote_txt(raw: str) -> str:
    """dnspython's TXT to_text() renders one or more quoted segments (e.g. '"a" "b"'); this joins
    them into the single logical string the record actually carries."""
    segments = _QUOTED_SEGMENT.findall(raw)
    return "".join(segments) if segments else raw


def _first_matching(values: tuple[str, ...], prefix: str) -> Optional[str]:
    """The first TXT value (unquoted) whose lower-cased text starts with prefix, or None."""
    for raw in values:
        text = _unquote_txt(raw)
        if text.lower().startswith(prefix):
            return text
    return None


def _build_spf(txt: RecordResult) -> SpfResult:
    if not txt.ok:
        return SpfResult(present=False, record=None, lookup_count=None, within_limit=None, error=txt.error)
    record = _first_matching(txt.values, "v=spf1")
    if record is None:
        return SpfResult(present=False, record=None, lookup_count=None, within_limit=None, error=None)
    lookup_count = len(_SPF_LOOKUP_MECH.findall(record))
    return SpfResult(
        present=True,
        record=record,
        lookup_count=lookup_count,
        within_limit=lookup_count <= _SPF_LOOKUP_LIMIT,
        error=None,
    )


def _build_dmarc(txt: RecordResult) -> DmarcResult:
    if not txt.ok:
        return DmarcResult(present=False, record=None, policy=None, error=txt.error)
    record = _first_matching(txt.values, "v=dmarc1")
    if record is None:
        return DmarcResult(present=False, record=None, policy=None, error=None)
    match = _DMARC_POLICY.search(record)
    return DmarcResult(present=True, record=record, policy=match.group(1).lower() if match else None, error=None)


def _build_dkim(txt: RecordResult) -> DkimResult:
    if not txt.ok:
        return DkimResult(present=False, record=None, error=txt.error)
    if not txt.values:
        return DkimResult(present=False, record=None, error=None)
    return DkimResult(present=True, record=_unquote_txt(txt.values[0]), error=None)


def _build_bimi(txt: RecordResult) -> BimiResult:
    if not txt.ok:
        return BimiResult(present=False, record=None, error=txt.error)
    record = _first_matching(txt.values, "v=bimi1")
    return BimiResult(present=record is not None, record=record, error=None)


def _fetch_mta_sts_policy(name: str) -> tuple[Optional[str], Optional[str]]:
    """(mode, error): fetches the MTA-STS policy file over HTTPS. error is an exception class name
    on failure, never a raw response body (may reflect attacker-controlled or misconfigured text)."""
    url = f"https://mta-sts.{name}/.well-known/mta-sts.txt"
    try:
        response = requests.get(url, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        _log.warning("mta-sts policy fetch failed for %s: %s", name, type(exc).__name__)
        return None, type(exc).__name__
    match = _MTA_STS_MODE.search(response.text)
    return (match.group(1) if match else None), None


def _build_mta_sts(name: str, txt: RecordResult) -> MtaStsResult:
    if not txt.ok:
        return MtaStsResult(txt_present=False, txt_record=None, policy_fetched=False, policy_mode=None, error=txt.error)
    record = _first_matching(txt.values, "v=stsv1")
    if record is None:
        return MtaStsResult(txt_present=False, txt_record=None, policy_fetched=False, policy_mode=None, error=None)
    policy_mode, fetch_error = _fetch_mta_sts_policy(name)
    return MtaStsResult(
        txt_present=True,
        txt_record=record,
        policy_fetched=policy_mode is not None,
        policy_mode=policy_mode,
        error=fetch_error,
    )


def _compute_flags(mx: RecordResult, spf: SpfResult, dmarc: DmarcResult) -> tuple[str, ...]:
    """A domain with no MX records receives no mail, so it should publish the most restrictive SPF
    and DMARC possible; anything looser leaves it open to being spoofed as a sender."""
    is_parked = mx.ok and not mx.values
    if not is_parked:
        return ()
    strict_spf = bool(spf.record) and spf.record.strip().lower() == "v=spf1 -all"
    strict_dmarc = dmarc.policy == "reject"
    if strict_spf and strict_dmarc:
        return ()
    return (_PARKED_FLAG,)


def mail_posture(name: str, resolver: Optional[_ResolverLike] = None) -> MailPosture:
    """
    Reads a domain's email-authentication posture: SPF, DMARC, DKIM (on common selectors),
    MTA-STS (TXT plus the published policy file) and BIMI.

    Args:
        name: A domain as normalize_domain() returns it (lower-case punycode).
        resolver: A dnspython-Resolver-like object; defaults to a real resolver built from
            domain_settings(). Tests inject a fake here instead of hitting the network; the
            MTA-STS policy fetch goes through this module's requests, which tests stub directly.

    Returns:
        MailPosture. Every part records ok/error state of its own rather than raising; flags
        carries any digest-worthy finding (currently only the parked-domain warning).
    """
    active_resolver = resolver or _make_resolver()
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        spf_future = pool.submit(_query_one, active_resolver, name, "TXT")
        dmarc_future = pool.submit(_query_one, active_resolver, f"_dmarc.{name}", "TXT")
        dkim_futures = {
            selector: pool.submit(_query_one, active_resolver, f"{selector}._domainkey.{name}", "TXT")
            for selector in DKIM_SELECTORS
        }
        mta_sts_future = pool.submit(_query_one, active_resolver, f"_mta-sts.{name}", "TXT")
        bimi_future = pool.submit(_query_one, active_resolver, f"default._bimi.{name}", "TXT")
        mx_future = pool.submit(_query_one, active_resolver, name, "MX")

        spf = _build_spf(spf_future.result())
        dmarc = _build_dmarc(dmarc_future.result())
        dkim = {selector: _build_dkim(future.result()) for selector, future in dkim_futures.items()}
        mta_sts_txt = mta_sts_future.result()
        bimi = _build_bimi(bimi_future.result())
        mx = mx_future.result()

    mta_sts = _build_mta_sts(name, mta_sts_txt)
    flags = _compute_flags(mx, spf, dmarc)
    return MailPosture(spf=spf, dmarc=dmarc, dkim=dkim, mta_sts=mta_sts, bimi=bimi, flags=flags)
