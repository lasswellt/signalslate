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
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors import dispatch  # noqa: E402
from pipeline.health import known_sources  # noqa: E402

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


def report(source: str, hours: int, limit: int, raw: bool) -> bool:
    until = utcnow()
    since = until - timedelta(hours=hours)

    print(f"\n=== {source} ===")
    print(f"window: {since:%Y-%m-%d %H:%M} .. {until:%Y-%m-%d %H:%M} UTC ({hours}h)")

    try:
        result = dispatch(source, since, until)
    except Exception as exc:  # noqa: BLE001 — a dry run reports crashes, it doesn't propagate them
        print(f"status: CRASHED — {type(exc).__name__}: {exc}")
        return False

    print(f"status: {result.status.upper()}")
    print(f"detail: {result.detail}")
    print(f"items:  {len(result.items)}")

    by_type: dict[str, int] = {}
    for item in result.items:
        by_type[item.item_type] = by_type.get(item.item_type, 0) + 1
    if by_type:
        print("        " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))

    for item in result.items[:limit]:
        print(f"\n  [{item.item_type}] {item.occurred_at:%Y-%m-%d %H:%M} id={item.external_id}")
        if raw:
            print(json.dumps(item.payload, indent=2, default=str)[:4000])
        else:
            print(f"  {preview(item.payload)}")

    if len(result.items) > limit:
        print(f"\n  ... {len(result.items) - limit} more (raise --limit to see them)")

    return result.status != "error"


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
