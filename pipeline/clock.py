"""
One source of truth for "now".

Everything stored in the DB is **naive UTC** — no tzinfo. That's the existing convention (Run.started_at,
SourceHealth.checked_at, CollectedItem.occurred_at) and changing it would break comparisons against rows
already on disk: Python raises TypeError when an aware datetime meets a naive one.

datetime.utcnow() produced exactly this value but is deprecated in 3.12, so this computes it the
supported way — aware UTC, then drop the tzinfo — and every caller goes through here.

Collectors computing a "last 24h" window MUST use this, not datetime.now(timezone.utc), or the
comparison against a stored watermark raises.
"""
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, matching what's stored in the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    """
    Formats a datetime as YYYY-MM-DDTHH:MM:SSZ, the literal the API returns and Graph/Zoom accept.

    dt: a naive UTC datetime (the storage convention), or None. A timezone-aware value is
    converted to UTC first rather than mislabelled. Sub-second precision is dropped: nothing in
    the UI needs it and a fixed width keeps the string trivially comparable.
    Returns None for None.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
