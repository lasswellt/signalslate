"""
Per-source collectors: pull the last 24h from each configured source.

Every collector is a function taking a window and returning a CollectionResult — never raising for
an expected failure (a dead token, a 429, a source that's down), because one bad source must not
cost the whole run. Unexpected exceptions are caught by execute_run's per-source handler.

Design decisions are in docs/_research/2026-09-13_phase2-collectors.md. The load-bearing one: these
use plain timestamp-filtered reads, not delta queries and not $batch. At one user and a 24h window,
delta buys nothing and costs a token store plus a resync path, and $batch buys nothing while adding
a failure mode where a 429 hides inside a 200 OK.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# How far back to look beyond the digest window. Covers a missed run and items that arrive late
# (Zoom widens this further — its summaries are generated well after a meeting ends).
DEFAULT_LOOKBACK = timedelta(hours=24)
OVERLAP = timedelta(minutes=30)


@dataclass
class Item:
    """One collected thing, ready to become a db.CollectedItem row."""
    item_type: str  # mail | event | task | chat | message | meeting
    external_id: str  # the source's own id, for dedupe across overlapping windows
    occurred_at: datetime  # naive UTC
    payload: dict


@dataclass
class CollectionResult:
    source: str
    status: str  # "ok" | "partial" | "error"
    detail: str
    items: list[Item] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "error"


def parse_iso(value: str) -> Optional[datetime]:
    """
    ISO 8601 from Graph/Zoom ("2026-09-13T20:48:29.832Z") to naive UTC.

    Naive to match the DB convention — see pipeline/clock.py. Returns None rather than raising:
    a single unparseable timestamp shouldn't cost the whole collection.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_slack_ts(value: str) -> Optional[datetime]:
    """Slack's "1616964509.832" (epoch seconds as a string) to naive UTC."""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def to_graph_time(value: datetime) -> str:
    """Naive UTC to the literal Graph wants in $filter and calendarView params."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def dispatch(source: str, since: datetime, until: datetime) -> "CollectionResult":
    """
    Run one source id's collector over an explicit window. No DB, no cursors, no side effects —
    so the runner and the dry-run CLI share exactly one dispatch table.

    Imports are local: importing this package must not drag in msal and every collector module.
    """
    from pipeline.collectors.graph import collect_m365
    from pipeline.collectors.slack import collect_slack
    from pipeline.collectors.zoom import collect_zoom
    from pipeline.health import slack_workspaces

    if source == "zoom":
        return collect_zoom(since, until)
    if source.startswith("m365_"):
        return collect_m365(source[len("m365_"):], since, until)
    if source.startswith("slack_"):
        label = source[len("slack_"):]
        return collect_slack(label, slack_workspaces().get(label), since, until)
    return CollectionResult(source, "error", f"No collector for source id {source!r}")
