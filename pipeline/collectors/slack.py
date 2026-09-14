"""
Slack collector: messages and thread replies since the window start, per workspace.

users.conversations -> conversations.history(oldest=) -> conversations.replies. No search API:
search.messages is legacy, and the Real-time Search API is query-driven, capped, and forbids
storing what it returns — neither fits "everything since yesterday morning".

Known limitation: replies are only fetched for thread parents that conversations.history returned,
i.e. parents whose own timestamp falls inside the window. A thread started last week that got a
dozen replies yesterday contributes nothing. Slack has no "threads updated since" endpoint without
the search APIs, so closing this needs persisted thread state — deferred, not overlooked.

Carries a rate-limit canary. Slack's 2025-05-29 change caps conversations.history at 1 req/min and
15 objects for apps "commercially distributed outside of the Marketplace", while stating that
"internal customer-built applications are not impacted". SignalSlate's per-workspace non-distributed
app is the latter, but Slack doesn't publish how an app gets classified — so rather than assume the
exemption, detect the capped state and say so.
"""
from datetime import datetime, timezone
from typing import Optional

import requests

from pipeline.collectors import CollectionResult, Item, parse_slack_ts
from pipeline.health import env_flag

SLACK = "https://slack.com/api"
TIMEOUT = 15
PAGE_SIZE = 200  # Slack's own recommended maximum
MAX_PAGES = 20
# A capped app gets exactly this many objects back regardless of the limit it asked for.
CAPPED_PAGE_SIZE = 15
# Never digest material. USLACK is the system user that has delivered notifications since
# 2026-06-17; USLACKBOT is legacy Slackbot, which still sends its own. Both, not either.
SYSTEM_USERS = {"USLACK", "USLACKBOT"}
# Conversation types to enumerate. im/mpim are dropped when SLACK_SKIP_DMS is set — asking for a
# type whose scope wasn't granted fails the whole users.conversations call with missing_scope.
CHANNEL_TYPES = ("public_channel", "private_channel", "mpim", "im")
DM_TYPES = {"mpim", "im"}


class SlackError(RuntimeError):
    pass


class RateLimitClassified(SlackError):
    """This app has been classified as rate-limit capped — the digest would be silently partial."""


def _call(token: str, method: str, params: dict) -> dict:
    try:
        resp = requests.get(
            f"{SLACK}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
        body = resp.json()
    except requests.RequestException as exc:
        raise SlackError(str(exc)) from exc
    except ValueError as exc:
        raise SlackError(f"{method}: non-JSON response") from exc

    if resp.status_code == 429:
        raise SlackError(f"{method}: rate limited, Retry-After={resp.headers.get('Retry-After', '?')}")
    if not body.get("ok"):
        raise SlackError(f"{method}: {body.get('error', 'unknown error')}")
    return body


def _paginate(token: str, method: str, params: dict) -> list[dict]:
    out: list[dict] = []
    cursor: Optional[str] = None

    for _ in range(MAX_PAGES):
        page_params = {**params, "limit": PAGE_SIZE}
        if cursor:
            page_params["cursor"] = cursor
        body = _call(token, method, page_params)
        batch = body.get("messages") or body.get("channels") or []
        out.extend(batch)

        # The canary: asked for 200, got exactly 15, and Slack says there's more. That is the
        # non-Marketplace cap, not a coincidence — bail loudly instead of digesting 7% of the day.
        if method == "conversations.history" and len(batch) == CAPPED_PAGE_SIZE and body.get("has_more"):
            raise RateLimitClassified(
                "app appears rate-limit classified (15 objects/request cap) — "
                "check that workspace distribution is disabled"
            )

        cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out


def _message_items(channel: dict, messages: list[dict], since: datetime) -> list[Item]:
    items = []
    for msg in messages:
        if msg.get("user") in SYSTEM_USERS or msg.get("subtype") in {"channel_join", "channel_leave"}:
            continue
        occurred = parse_slack_ts(msg.get("ts", ""))
        if occurred is None or occurred < since:
            continue
        payload = {
            **msg,
            "channelId": channel.get("id"),
            "channelName": channel.get("name"),
            "isIm": bool(channel.get("is_im")),
        }
        items.append(Item("message", f"{channel.get('id')}:{msg.get('ts')}", occurred, payload))
    return items


def collect_slack(label: str, token: Optional[str], since: datetime, until: datetime) -> CollectionResult:
    source = f"slack_{label}"
    if not token:
        return CollectionResult(source, "error", f"No token in .env for {label}")

    # Must match what the health check requires, or a user who sets this gets a green check and
    # zero items: users.conversations rejects the whole call if any requested type lacks its scope.
    skip_dms = env_flag("SLACK_SKIP_DMS")

    # since is naive UTC (pipeline/clock.py). datetime.timestamp() reads a naive value as LOCAL
    # time, so without this the window start shifts by the container's UTC offset and the first
    # hours of every window vanish from the digest.
    oldest = f"{since.replace(tzinfo=timezone.utc).timestamp():.6f}"

    try:
        types = [t for t in CHANNEL_TYPES if not (skip_dms and t in DM_TYPES)]
        channels = _paginate(
            token,
            "users.conversations",
            {"types": ",".join(types), "exclude_archived": "true"},
        )
    except SlackError as exc:
        return CollectionResult(source, "error", str(exc))

    items: list[Item] = []
    failures: list[str] = []
    thread_count = 0

    for channel in channels:
        try:
            messages = _paginate(token, "conversations.history", {"channel": channel["id"], "oldest": oldest})
        except RateLimitClassified as exc:
            # Affects every channel, not just this one — stop and report rather than continue
            # collecting a knowingly incomplete picture.
            return CollectionResult(source, "error", str(exc), items)
        except SlackError as exc:
            failures.append(f"{channel.get('name', channel['id'])}: {exc}")
            continue

        items.extend(_message_items(channel, messages, since))

        # Only parents with replies need a second call — reply_count is on the parent, so this
        # costs nothing to check.
        for msg in messages:
            if not msg.get("reply_count"):
                continue
            try:
                replies = _paginate(
                    token,
                    "conversations.replies",
                    {"channel": channel["id"], "ts": msg["ts"], "oldest": oldest},
                )
            except SlackError as exc:
                failures.append(f"thread {msg.get('ts')}: {exc}")
                continue
            thread_count += 1
            # replies includes the parent; it's already collected above.
            items.extend(_message_items(channel, [r for r in replies if r.get("ts") != msg.get("ts")], since))

    detail = f"{len(items)} messages across {len(channels)} conversations, {thread_count} threads"
    if failures:
        return CollectionResult(source, "partial", f"{detail} ({len(failures)} failed: {failures[0]})", items)
    return CollectionResult(source, "ok", detail, items)
