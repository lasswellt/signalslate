"""
Output formatting shared by the API routers.

Every stored datetime is naive UTC (pipeline.clock.utcnow). Serialized bare, it becomes
"2026-09-21T10:00:00" with no zone, and browsers parse that as LOCAL time, so a timestamp shown
in the UI would be off by the viewer's UTC offset. Sending an explicit Z removes the ambiguity.
"""
from datetime import datetime, timezone
from typing import Optional


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    """
    Formats a datetime as YYYY-MM-DDTHH:MM:SSZ.

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
