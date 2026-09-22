"""
RDAP registration-data lookups for the domains subsystem (Research Finding 5).

Design decisions:

- whoisit.bootstrap() downloads IANA's ASN/DNS/IP/entity RDAP maps (five HTTP requests) once per
  process; whoisit.is_bootstrapped() makes repeat calls a no-op, but nothing in whoisit documents
  the bootstrap step itself as thread-safe, so lookup_rdap() gates the first bootstrap behind a
  module-level lock (check-lock-check) rather than trusting concurrent callers not to race it.
- A TLD absent from IANA's bootstrap map (some ccTLDs, e.g. .de, publish WHOIS only, no RDAP) is
  not a lookup failure: whoisit raises UnsupportedError for it, which this module reports as
  status="unsupported" so callers can tell "we don't support this TLD" apart from "the lookup
  broke".
- A 404 from the registry (whoisit's ResourceDoesNotExist) means the RDAP server has no record for
  the name, most often because it is unregistered — but RDAP registries are not always
  authoritative about availability (a just-released or reserved name can 404 too), so the research
  doc treats not_found as a signal to feed into a separate availability check, not proof on its
  own.
- Every other whoisit/network failure becomes status="error" with only the exception CLASS name
  recorded (pipeline.redact / pipeline.runner convention: never log or surface an exception
  message, which can carry request/response detail).
- whoisit is a functional API over process-global bootstrap state, so there is no "client" object
  the way dns.py injects a resolver; instead the three whoisit entry points this module calls
  (bootstrap, is_bootstrapped, domain) are keyword parameters defaulting to the real whoisit
  functions, so tests can stub them with no network access and no monkeypatching of module
  globals.
- RDAP domain status values are RFC 8056's lower-case, space-separated mapping of the EPP status
  vocabulary (e.g. "client transfer prohibited" for EPP's clientTransferProhibited). This module
  normalizes each one back to that EPP camelCase form, which is what operators and the digest UI
  recognise. locked is true when any *TransferProhibited status (client- or server-side) is
  present — the signal that actually blocks a transfer, as opposed to update/delete prohibitions.
"""
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import whoisit
from whoisit.errors import ResourceDoesNotExist, UnsupportedError, WhoisItError

_log = logging.getLogger(__name__)

_bootstrap_lock = threading.Lock()


@dataclass(frozen=True)
class RdapResult:
    """One domain's RDAP outcome. status="ok" is the only status where the remaining fields carry
    real data; every other status leaves them at their empty defaults and, for "error", records
    the exception class name."""
    status: str  # "ok" | "not_found" | "unsupported" | "error"
    registrar: Optional[str]
    created: Optional[datetime]
    expires: Optional[datetime]
    nameservers: tuple[str, ...]
    statuses: tuple[str, ...]
    locked: bool
    error: Optional[str]


def _empty_result(status: str, error: Optional[str] = None) -> RdapResult:
    return RdapResult(
        status=status,
        registrar=None,
        created=None,
        expires=None,
        nameservers=(),
        statuses=(),
        locked=False,
        error=error,
    )


def _camel_case_status(raw: str) -> str:
    """RDAP's "client transfer prohibited" (RFC 8056 space-separated mapping) to the EPP
    camelCase form "clientTransferProhibited". Already-camelCase or single-word input passes
    through unchanged (lower-cased first word only)."""
    words = raw.replace("-", " ").split()
    if not words:
        return ""
    first, *rest = words
    return first[:1].lower() + first[1:] + "".join(word[:1].upper() + word[1:] for word in rest if word)


def _is_locked(raw_statuses: tuple[str, ...]) -> bool:
    """True when any status is a client- or server-side transfer prohibition — the one that
    actually blocks a transfer, per RFC 8056's client.../server... status pairs."""
    return any(status.strip().lower().endswith("transfer prohibited") for status in raw_statuses)


def _extract_registrar(parsed: dict) -> Optional[str]:
    """The first "registrar" role entity's display name, falling back to its handle when whoisit
    could not parse a vCard name (registries vary in how much entity detail they publish)."""
    entities = parsed.get("entities") or {}
    registrars = entities.get("registrar") or []
    if not registrars:
        return None
    first = registrars[0]
    return first.get("name") or first.get("handle") or None


def _ensure_bootstrapped(
    bootstrap: Callable[..., None],
    is_bootstrapped: Callable[[], bool],
) -> None:
    """Bootstraps whoisit's IANA RDAP maps once per process. Thread-safe check-lock-check: only
    the first caller to see is_bootstrapped() False ever makes the bootstrap requests."""
    if is_bootstrapped():
        return
    with _bootstrap_lock:
        if not is_bootstrapped():
            bootstrap()


def lookup_rdap(
    name: str,
    *,
    domain_lookup: Callable[[str], dict] = whoisit.domain,
    bootstrap: Callable[..., None] = whoisit.bootstrap,
    is_bootstrapped: Callable[[], bool] = whoisit.is_bootstrapped,
) -> RdapResult:
    """
    Looks up RDAP registration data for name via whoisit, bootstrapping whoisit's IANA RDAP maps
    once per process on first use.

    Args:
        name: A domain as normalize_domain() returns it (lower-case punycode).
        domain_lookup: A whoisit.domain-like callable; tests inject a stub instead of hitting the
            network. Defaults to whoisit.domain.
        bootstrap: A whoisit.bootstrap-like callable; tests inject a stub. Defaults to
            whoisit.bootstrap.
        is_bootstrapped: A whoisit.is_bootstrapped-like callable; tests inject a stub. Defaults to
            whoisit.is_bootstrapped.

    Returns:
        An RdapResult. Never raises: bootstrap failures, an unsupported TLD, a 404 and any other
        whoisit/network failure all come back as a typed status rather than a raised exception.
    """
    try:
        _ensure_bootstrapped(bootstrap, is_bootstrapped)
    except WhoisItError as exc:
        _log.warning("rdap bootstrap failed: %s", type(exc).__name__)
        return _empty_result("error", type(exc).__name__)

    try:
        parsed = domain_lookup(name)
    except UnsupportedError:
        return _empty_result("unsupported")
    except ResourceDoesNotExist:
        return _empty_result("not_found")
    except WhoisItError as exc:
        _log.warning("rdap lookup failed for %s: %s", name, type(exc).__name__)
        return _empty_result("error", type(exc).__name__)
    except Exception as exc:  # network/transport failures whoisit does not wrap
        _log.warning("rdap lookup failed for %s: %s", name, type(exc).__name__)
        return _empty_result("error", type(exc).__name__)

    raw_statuses = tuple(parsed.get("status") or ())
    return RdapResult(
        status="ok",
        registrar=_extract_registrar(parsed),
        created=parsed.get("registration_date"),
        expires=parsed.get("expiration_date"),
        nameservers=tuple(parsed.get("nameservers") or ()),
        statuses=tuple(_camel_case_status(status) for status in raw_statuses if status),
        locked=_is_locked(raw_statuses),
        error=None,
    )
