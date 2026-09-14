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


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, matching what's stored in the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
