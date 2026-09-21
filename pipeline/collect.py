"""
Dry-run a collector against real credentials without touching the database.

Exists because the collectors' knowledge of each API's response shape comes from documentation, not
from live calls. This is how you find out what the APIs actually return — before a scheduled run
writes a day of misparsed items.

    python -m pipeline.collect                    # list declared sources
    python -m pipeline.collect slack_work         # last 24h, summary only
    python -m pipeline.collect m365_work --hours 2 --limit 3
    python -m pipeline.collect zoom --raw         # full payloads, for checking field names
    python -m pipeline.collect --all

Nothing here writes: no CollectedItem rows, no cursor advance, no Run. Safe to run against
production any number of times.
"""
import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors import dispatch  # noqa: E402
from pipeline.health import known_sources  # noqa: E402

# hours mirrors collectors.MAX_BACKFILL (7 days); the payload cap is the CLI's long-standing --raw cut.
MAX_HOURS = 168
MAX_LIMIT = 50
RAW_PAYLOAD_CHARS = 4000
PREVIEW_FIELDS = ("subject", "text", "title", "displayName", "topic", "summary_content")


def preview(payload: dict) -> str:
    """One line that says what an item actually is, whatever source it came from."""
    for field in PREVIEW_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:100]
    body = payload.get("body")
    if isinstance(body, dict) and body.get("content"):
        return str(body["content"]).strip().replace("\n", " ")[:100]
    return "(no preview field)"


def _execute(source: str, hours: int, limit: int, raw: bool) -> tuple[dict, str]:
    """
    The collector call and its shaping, with no argument checks. Returns the dry_run() dict plus the
    crash message ("" unless the collector crashed).

    The message is a separate return value, not a dict key: it may quote a response body, so it may
    only reach the CLI operator's terminal, never dry_run()'s caller (the API). The CLI's crash line
    has always carried it, so report() keeps printing it.
    """
    started = time.perf_counter()
    until = utcnow()
    since = until - timedelta(hours=hours)
    data: dict = {
        "source": source,
        "status": "crashed",
        "detail": "",
        "window": {"since": since, "until": until, "hours": hours},
        "count": 0,
        "by_type": {},
        "items": [],
        "duration_ms": 0,
    }
    crash_message = ""

    try:
        result = dispatch(source, since, until)
    except Exception as exc:  # noqa: BLE001 — a dry run reports crashes, it doesn't propagate them
        data["detail"] = type(exc).__name__
        crash_message = str(exc)
    else:
        by_type: dict[str, int] = {}
        for item in result.items:
            by_type[item.item_type] = by_type.get(item.item_type, 0) + 1
        items = []
        for item in result.items[:limit]:
            entry = {
                "item_type": item.item_type,
                "occurred_at": item.occurred_at,
                "external_id": item.external_id,
                "preview": preview(item.payload),
            }
            if raw:
                entry["payload"] = json.dumps(item.payload, indent=2, default=str)[:RAW_PAYLOAD_CHARS]
            items.append(entry)
        data.update(
            status=result.status,
            detail=result.detail,
            count=len(result.items),
            by_type=by_type,
            items=items,
        )

    data["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return data, crash_message


def dry_run(source: str, hours: int = 24, limit: int = 5, *, raw: bool = False) -> dict:
    """
    Run one collector over a recent window and return what it found, writing nothing.

    Datetimes in the result are naive UTC, like everywhere else in the pipeline; whoever serialises
    the dict is responsible for the Z. A collector crash comes back as status "crashed" with the
    exception CLASS NAME as detail and nothing else, because an exception message can quote a
    response body that carries a credential and this dict is meant to reach the API.

    Args:
        source: A source id from known_sources().
        hours: Lookback window, clamped to 1..168 (the MAX_BACKFILL ceiling).
        limit: How many items to include, clamped to 0..50; count and by_type cover all of them.
        raw: Also include each listed item's payload as JSON, truncated to RAW_PAYLOAD_CHARS.

    Returns:
        {source, status, detail, window: {since, until, hours}, count, by_type, items, duration_ms}
        where status is ok | partial | error | crashed.

    Raises:
        ValueError: source is not declared.
    """
    if source not in known_sources():
        raise ValueError(f"unknown source: {source}")
    data, _ = _execute(source, min(max(hours, 1), MAX_HOURS), min(max(limit, 0), MAX_LIMIT), raw)
    return data


def report(source: str, hours: int, limit: int, raw: bool) -> bool:
    """
    Print a dry run for the CLI; True unless the source errored or crashed.

    Goes through _execute, not dry_run: main() has already checked the source against .env, and the
    CLI has never clamped --hours or --limit, so its output must not change for them either.
    """
    data, crash_message = _execute(source, hours, limit, raw)
    window = data["window"]

    print(f"\n=== {source} ===")
    print(f"window: {window['since']:%Y-%m-%d %H:%M} .. {window['until']:%Y-%m-%d %H:%M} UTC ({hours}h)")

    if data["status"] == "crashed":
        print(f"status: CRASHED — {data['detail']}: {crash_message}")
        return False

    print(f"status: {data['status'].upper()}")
    print(f"detail: {data['detail']}")
    print(f"items:  {data['count']}")

    if data["by_type"]:
        print("        " + ", ".join(f"{k}={v}" for k, v in sorted(data["by_type"].items())))

    for item in data["items"]:
        print(f"\n  [{item['item_type']}] {item['occurred_at']:%Y-%m-%d %H:%M} id={item['external_id']}")
        if raw:
            print(item["payload"])
        else:
            print(f"  {item['preview']}")

    if data["count"] > limit:
        print(f"\n  ... {data['count'] - limit} more (raise --limit to see them)")

    return data["status"] != "error"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run one or more collectors against real credentials. Writes nothing."
    )
    parser.add_argument("source", nargs="?", help="source id, e.g. m365_work, slack_work, zoom")
    parser.add_argument("--all", action="store_true", help="every declared source")
    parser.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    parser.add_argument("--limit", type=int, default=5, help="items to print per source (default 5)")
    parser.add_argument("--raw", action="store_true", help="dump full JSON payloads — use this to verify field names")
    args = parser.parse_args()

    declared = known_sources()

    if args.all:
        targets = declared
    elif args.source:
        targets = [args.source]
    else:
        print("Declared sources (from .env):")
        for source in declared:
            print(f"  {source}")
        print("\nPass one, or --all. Nothing is written to the database.")
        return 0

    unknown = [t for t in targets if t not in declared]
    if unknown:
        print(f"Not declared in .env: {', '.join(unknown)}")
        print(f"Known: {', '.join(declared)}")
        return 2

    results = [report(t, args.hours, args.limit, args.raw) for t in targets]
    print()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
