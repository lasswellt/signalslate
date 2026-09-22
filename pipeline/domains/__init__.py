"""
Domain-collector shared primitives: input normalization and the module's config.

Design decisions:

- normalize_domain() is the one place user/API input becomes a domain name every other module
  trusts. It IDNA-encodes to punycode (the wire form DNS, RDAP and every registrar API expect) so
  a duplicate-detection compare, a DB lookup and an outbound API call never disagree on the same
  domain typed three different ways (unicode vs punycode, trailing dot, mixed case).
- domain_settings() reads through pipeline.health.env(), the same overlay-aware config surface
  every other source uses, instead of os.environ directly (project convention). Purchasing is a
  spend-money operation, so any config that fails to parse must fail CLOSED (purchase disabled)
  rather than falling back to a default that could silently re-enable spending.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

import idna

from pipeline import health

_log = logging.getLogger(__name__)

DEFAULT_MAX_PRICE = Decimal("25")
DEFAULT_DAILY_CAP = Decimal("50")
DEFAULT_REFRESH_CRON = "30 5 * * *"
_MAX_LABEL_LENGTH = 63
_MAX_DOMAIN_LENGTH = 253


def normalize_domain(raw: str) -> tuple[str, str]:
    """
    Normalizes a user-supplied domain to (punycode_name, tld).

    Strips whitespace, lower-cases, drops one trailing dot, then IDNA/UTS-46-encodes to punycode.
    Rejects a scheme or path (a pasted URL, not a domain), any label over 63 octets and a total
    over 253 — the same limits idna.encode() already enforces on the pre-encode string, checked
    again here against the final ASCII form so the punycode expansion of a long unicode label
    cannot slip past.

    Args:
        raw: The domain as typed or imported, unicode or ASCII, with or without a trailing dot.

    Returns:
        (name, tld): name is the full lower-case punycode domain; tld is its last label.

    Raises:
        ValueError: Empty input, a scheme/path, or the domain fails IDNA validation or a length
            limit.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("domain must not be empty")
    candidate = raw.strip()
    if "://" in candidate or "/" in candidate:
        raise ValueError(f"domain must not include a scheme or path: {raw!r}")
    candidate = candidate.lower()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if not candidate:
        raise ValueError("domain must not be empty")

    try:
        encoded = idna.encode(candidate, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        raise ValueError(f"invalid domain {raw!r}: {exc}") from exc

    labels = encoded.split(".")
    if len(labels) < 2:
        raise ValueError(f"domain must include a TLD: {raw!r}")
    if any(len(label) > _MAX_LABEL_LENGTH for label in labels):
        raise ValueError(f"domain label exceeds {_MAX_LABEL_LENGTH} characters: {raw!r}")
    if len(encoded) > _MAX_DOMAIN_LENGTH:
        raise ValueError(f"domain exceeds {_MAX_DOMAIN_LENGTH} characters: {raw!r}")

    return encoded, labels[-1]


@dataclass(frozen=True)
class DomainSettings:
    """The domain-collector's config, resolved once per call so every reader agrees within a run."""
    purchase_enabled: bool
    max_price: Decimal
    daily_cap: Decimal
    allow_premium: bool
    refresh_cron: str
    resolver: Optional[str]


def _decimal_env(env: dict, key: str, default: Decimal) -> tuple[Decimal, bool]:
    """(value, ok): the parsed Decimal, or (default, False) when unset or not a valid number."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default, True
    try:
        return Decimal(raw), True
    except InvalidOperation:
        _log.warning("domain_settings: %s is not a valid number, using default", key)
        return default, False


def domain_settings() -> DomainSettings:
    """
    Reads DOMAINS_* config through pipeline.health.env().

    A malformed DOMAINS_MAX_PRICE or DOMAINS_DAILY_CAP forces purchase_enabled False regardless of
    DOMAINS_PURCHASE_ENABLED: a config typo must never turn into an uncapped or unbounded spend.

    Returns:
        DomainSettings with every field defaulted, so callers never see a missing key.
    """
    env = health.env()
    max_price, max_price_ok = _decimal_env(env, "DOMAINS_MAX_PRICE", DEFAULT_MAX_PRICE)
    daily_cap, daily_cap_ok = _decimal_env(env, "DOMAINS_DAILY_CAP", DEFAULT_DAILY_CAP)
    purchase_enabled = health.env_flag("DOMAINS_PURCHASE_ENABLED") and max_price_ok and daily_cap_ok

    refresh_cron = (env.get("DOMAINS_REFRESH_CRON") or "").strip() or DEFAULT_REFRESH_CRON
    resolver = (env.get("DOMAINS_RESOLVER") or "").strip() or None

    return DomainSettings(
        purchase_enabled=purchase_enabled,
        max_price=max_price,
        daily_cap=daily_cap,
        allow_premium=health.env_flag("DOMAINS_ALLOW_PREMIUM"),
        refresh_cron=refresh_cron,
        resolver=resolver,
    )
