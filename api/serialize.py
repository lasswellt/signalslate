"""
Output formatting shared by the API routers.

Every stored datetime is naive UTC (pipeline.clock.utcnow). Serialized bare, it becomes
"2026-09-21T10:00:00" with no zone, and browsers parse that as LOCAL time, so a timestamp shown
in the UI would be off by the viewer's UTC offset. iso_z (pipeline.clock) sends an explicit Z.
"""
from typing import Optional

from pipeline.clock import iso_z
from pipeline.redact import redact

__all__ = ["MAX_TEXT", "iso_z", "scrub"]

# Same cap the runner applies to text it persists: applied after redaction, never before. Free text
# on a run, a health row or a dry-run payload can quote a provider response that carries a credential.
MAX_TEXT = 1000


def scrub(text: Optional[str], known: list[str]) -> Optional[str]:
    """redact() with the known secret values and the MAX_TEXT cap; None passes through. Never raises."""
    if text is None:
        return None
    return redact(text, known, max_len=MAX_TEXT)
