"""
Package-tracking shared primitives: the status model and the module's config.

Design decisions:

- STATUSES is one ordered tuple and STATUS_RANK derives from it, so a status added in one place
  cannot be missing from the other. Ranked progression is pending < in_transit <
  out_for_delivery < delivered; "returned" is terminal but off the happy path, so it is unranked.
- Ranking rules: a ranked status only moves forward, never back (a late-arriving older email must
  not demote a delivered parcel). "exception" may follow any non-terminal status and may be
  followed by any ranked status (carrier recovered). "stale" and "unknown" are side states set by
  the system when nothing is known; they never overwrite a ranked status and any ranked status
  replaces them. Terminal statuses (delivered, returned) are never overwritten.
- packages_settings() reads through pipeline.health.env(), the overlay-aware config surface, not
  os.environ (project convention). Malformed values fall back to the default with a warning
  rather than raising, so a typo cannot stop the scheduler from starting.
- Carrier credentials are repr=False so a logged or traced settings object never leaks a secret.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from apscheduler.triggers.cron import CronTrigger

from pipeline import health

_log = logging.getLogger(__name__)

STATUSES: tuple[str, ...] = (
    "pending", "in_transit", "out_for_delivery", "delivered", "returned",
    "exception", "stale", "unknown",
)
_RANKED: tuple[str, ...] = ("pending", "in_transit", "out_for_delivery", "delivered")
STATUS_RANK: dict[str, int] = {name: i for i, name in enumerate(_RANKED)}
TERMINAL_STATUSES: frozenset[str] = frozenset({"delivered", "returned"})
SIDE_STATUSES: frozenset[str] = frozenset({"exception", "stale", "unknown"})

DEFAULT_REFRESH_CRON = "23 */2 * * *"
DEFAULT_SCAN_DAYS = 60
DEFAULT_STALE_DAYS = 30
DEFAULT_POLL_INTERVAL_HOURS = 2
DEFAULT_STOP_AFTER_DELIVERED_HOURS = 48


@dataclass(frozen=True)
class PackagesSettings:
    """The package-tracking config, resolved once per call so every reader agrees within a run."""
    refresh_cron: str = DEFAULT_REFRESH_CRON
    scan_days: int = DEFAULT_SCAN_DAYS
    stale_days: int = DEFAULT_STALE_DAYS
    poll_interval_hours: int = DEFAULT_POLL_INTERVAL_HOURS
    stop_after_delivered_hours: int = DEFAULT_STOP_AFTER_DELIVERED_HOURS
    ups_client_id: Optional[str] = field(default=None, repr=False)
    ups_client_secret: Optional[str] = field(default=None, repr=False)
    usps_client_id: Optional[str] = field(default=None, repr=False)
    usps_client_secret: Optional[str] = field(default=None, repr=False)

    @property
    def ups_enabled(self) -> bool:
        """True when both UPS credentials are configured."""
        return bool(self.ups_client_id and self.ups_client_secret)

    @property
    def usps_enabled(self) -> bool:
        """True when both USPS credentials are configured."""
        return bool(self.usps_client_id and self.usps_client_secret)


def _int_env(env: dict, key: str, default: int, low: int, high: int) -> int:
    """The int in [low, high] at key, or default (with a warning) when malformed or out of range."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.warning("packages_settings: %s is not an integer, using default", key)
        return default
    if not low <= value <= high:
        _log.warning("packages_settings: %s outside %d..%d, using default", key, low, high)
        return default
    return value


def _cron_env(env: dict, key: str, default: str) -> str:
    """The valid 5-field crontab at key, or default (with a warning) when blank or invalid."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        CronTrigger.from_crontab(raw)
    except ValueError:
        _log.warning("packages_settings: invalid %s, using default", key)
        return default
    return raw


def _opt_env(env: dict, key: str) -> Optional[str]:
    """The stripped value at key, or None when unset or blank."""
    return (env.get(key) or "").strip() or None


def packages_settings() -> PackagesSettings:
    """
    Reads PACKAGES_*, UPS_* and USPS_* config through pipeline.health.env().

    Returns:
        PackagesSettings with every field defaulted, so callers never see a missing key.
    """
    env = health.env()
    return PackagesSettings(
        refresh_cron=_cron_env(env, "PACKAGES_REFRESH_CRON", DEFAULT_REFRESH_CRON),
        scan_days=_int_env(env, "PACKAGES_SCAN_DAYS", DEFAULT_SCAN_DAYS, 1, 365),
        stale_days=_int_env(env, "PACKAGES_STALE_DAYS", DEFAULT_STALE_DAYS, 1, 365),
        ups_client_id=_opt_env(env, "UPS_CLIENT_ID"),
        ups_client_secret=_opt_env(env, "UPS_CLIENT_SECRET"),
        usps_client_id=_opt_env(env, "USPS_CLIENT_ID"),
        usps_client_secret=_opt_env(env, "USPS_CLIENT_SECRET"),
    )
