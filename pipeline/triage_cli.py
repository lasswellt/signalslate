"""
Judge the map (triage) call against real collected Gmail items, without writing anything.

Exists because the triage prompt and schema were designed against synthetic mail. This shows what
the model would actually be sent for your real inbox, and, when you choose to spend the API credit,
what it returns for it.

    python -m pipeline.triage_cli gmail_personal --dry            # the request, no API key, no network
    python -m pipeline.triage_cli gmail_personal --hours 48 --limit 10
    python -m pipeline.triage_cli gmail_personal --batch-size 5 --dry

--dry prints the first batch's request exactly as it would be sent. Without --dry the item text
(subjects, senders, bodies) is sent to the Anthropic API and every window item is triaged: that call
costs credit, so it is a manual step. Items are read from what a run already stored; this never
collects, and it writes nothing: no rows, no cursor, no Run.
"""
import argparse
import json
import math
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.health import llm_settings  # noqa: E402
from pipeline.normalize import NoAdapterError, NormalizedItem, PayloadError, normalize  # noqa: E402
from pipeline.triage import (  # noqa: E402
    BATCH_SIZE,
    TriageConfigError,
    assign_aliases,
    batches,
    build_request,
    make_client,
    triage_items,
)

ADAPTER_PREFIX = "gmail_"


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def load_items(source: str, hours: int) -> tuple[list[NormalizedItem], int] | None:
    """
    Normalized items for `source` in the last `hours`, plus how many rows were skipped as unreadable.

    Returns None (after printing why) when the source has no adapter.
    """
    since = utcnow() - timedelta(hours=hours)
    rows = db.items_for_source_since(source, since)

    items: list[NormalizedItem] = []
    skipped = 0
    for row in rows:
        try:
            items.append(normalize(row.source, row.item_type, row.external_id, row.occurred_at, row.payload))
        except PayloadError:
            skipped += 1
        except NoAdapterError as exc:
            print(f"{exc}: only {ADAPTER_PREFIX}* sources can be triaged so far")
            return None
    return items, skipped


def print_dry(items: list[NormalizedItem], batch_size: int) -> None:
    """The first batch's request as JSON: the schema instead of the class, the user turn parsed for reading."""
    model = llm_settings()["map_model"]
    pairs, _ = assign_aliases(items)
    first = next(batches(pairs, batch_size))
    request = build_request(model, first)

    printable = {
        **request,
        "output_format": request["output_format"].model_json_schema(),
        "messages": [
            {"role": m["role"], "content": json.loads(m["content"])} for m in request["messages"]
        ],
    }
    print(f"model:   {model}")
    print(f"items:   {len(items)} in {math.ceil(len(items) / batch_size)} batch(es) of up to {batch_size}")
    print(f"showing: batch 1 ({len(first)} items). No API key used, no network call made.\n")
    print(json.dumps(printable, indent=2, ensure_ascii=False))


def print_live(items: list[NormalizedItem], batch_size: int, limit: int) -> int:
    """Call the model for every item, print up to `limit` records. 1 only when every item was stubbed."""
    try:
        client = make_client()
    except TriageConfigError as exc:
        print(f"cannot triage: {exc}")
        return 2

    print(f"WARNING: sending the text of {len(items)} item(s) to the Anthropic API; this spends API credit.")
    result = triage_items(items, client, batch_size=batch_size)

    for triaged in result.items[:limit]:
        record = triaged.record
        print(f"\n{triaged.item_id}{' (stub)' if triaged.stubbed else ''}")
        print(f"  {record.category.value} / {record.importance.value}: {record.one_line}")
        if record.action_text:
            print(f"  action: {record.action_text}" + (f" (due {record.due})" if record.due else ""))
        elif record.due:
            print(f"  due: {record.due}")
    if len(result.items) > limit:
        print(f"\n  ... {len(result.items) - limit} more (raise --limit to see them)")

    print(f"\nstubbed: {result.stubbed_count} of {len(result.items)}")
    for failure in result.failures:
        print(f"failure: {failure}")
    return 1 if result.stubbed_count == len(result.items) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the triage map call over collected Gmail items. Writes nothing."
    )
    parser.add_argument("source", help="stored source id, e.g. gmail_personal")
    parser.add_argument("--hours", type=_positive_int, default=24, help="lookback window (default 24)")
    parser.add_argument("--limit", type=_positive_int, default=5, help="records to print (default 5)")
    parser.add_argument(
        "--batch-size", type=_positive_int, default=BATCH_SIZE, help=f"items per call (default {BATCH_SIZE})"
    )
    parser.add_argument("--dry", action="store_true", help="print the first request only: no API key, no network")
    args = parser.parse_args(argv)

    if not args.source.startswith(ADAPTER_PREFIX):
        print(f"no normalizer for source {args.source!r}: only {ADAPTER_PREFIX}* sources can be triaged so far")
        return 2

    # Checked before any query: SQLite would otherwise create an empty file at the configured path.
    database = db.engine.url.database
    if not database or not Path(database).exists():
        print("no database yet: items are stored by a run, not by `python -m pipeline.collect`")
        return 2

    loaded = load_items(args.source, args.hours)
    if loaded is None:
        return 2
    items, skipped = loaded
    if skipped:
        print(f"skipped {skipped} row(s) with an unreadable payload")
    if not items:
        print(f"no {args.source} items in the last {args.hours}h")
        return 0

    if args.dry:
        print_dry(items, args.batch_size)
        return 0
    return print_live(items, args.batch_size, args.limit)


if __name__ == "__main__":
    sys.exit(main())
