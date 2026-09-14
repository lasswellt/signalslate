"""
Microsoft Graph collectors: mail, calendar, Microsoft To Do, Teams 1:1/group chat.

One tenant alias per call. All four run under the delegated scopes already consented in Phase 1
(Mail.Read, Calendars.Read, Tasks.Read, Chat.Read) — no new consent, no app-only permissions, and
none of these endpoints are metered.

Deliberately no delta queries and no $batch; see this package's docstring.
"""
from datetime import datetime
from typing import Iterator, Optional

import msal
import requests

from pipeline.collectors import CollectionResult, Item, parse_iso, to_graph_time
from pipeline.health import SCOPES, m365_app, m365_cache_path, m365_tenant_config

GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 15
# Graph caps $top at 50 on the chat endpoints; the others allow more but there's no reason to
# differ — a day's worth of anything fits in a page or two.
PAGE_SIZE = 50
# Stops a pathological filter (or a tenant with a decade of unread mail) from paging forever.
MAX_PAGES = 40


class GraphError(RuntimeError):
    """A Graph call failed in a way the caller should report, not retry."""


def access_token(alias: str) -> str:
    """
    A live Graph token for one tenant, via the same MSAL cache Phase 1's health check refreshes.

    Reuses pipeline.health's cache path and app construction rather than rebuilding either — the
    tenant-specific authority matters (a /common authority caches under the real tenant and misses).
    """
    cfg = m365_tenant_config(alias)
    if cfg is None:
        raise GraphError(f"No usable .env entry for alias={alias!r}")

    cache_path = m365_cache_path(alias)
    if not cache_path.exists():
        raise GraphError("No token cache — run auth/m365_bootstrap.py once on a machine with a browser")

    cache = msal.SerializableTokenCache()
    cache.deserialize(cache_path.read_text())
    app = m365_app(cfg, cache)

    accounts = app.get_accounts()
    if not accounts:
        raise GraphError("Cache has no account — re-run auth/m365_bootstrap.py")

    result = app.acquire_token_silent_with_error(SCOPES, account=accounts[0])
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize())

    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or (result or {}).get("error") or "silent refresh failed"
        raise GraphError(detail)
    return result["access_token"]


def get_pages(token: str, url: str, params: Optional[dict] = None) -> Iterator[dict]:
    """
    Yield each page of a Graph collection, following @odata.nextLink.

    nextLink already carries every query parameter, so params go on the first request only —
    re-sending them alongside a nextLink is how you get a page that silently ignores your filter.
    """
    headers = {"Authorization": f"Bearer {token}"}
    next_url: Optional[str] = url
    first = True

    for _ in range(MAX_PAGES):
        if next_url is None:
            return
        try:
            resp = requests.get(
                next_url,
                headers=headers,
                params=params if first else None,
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise GraphError(str(exc)) from exc

        if resp.status_code == 429:
            # One daily pull shouldn't reach this. If it does, stop rather than hammer — the
            # next run picks the window up from the cursor.
            raise GraphError(f"throttled (429), Retry-After={resp.headers.get('Retry-After', '?')}")
        if resp.status_code >= 400:
            raise GraphError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        body = resp.json()
        yield body
        next_url = body.get("@odata.nextLink")
        first = False


def _collect(token: str, url: str, params: dict) -> list[dict]:
    out: list[dict] = []
    for page in get_pages(token, url, params):
        out.extend(page.get("value", []))
    return out


def collect_mail(token: str, since: datetime, until: datetime) -> list[Item]:
    rows = _collect(
        token,
        f"{GRAPH}/me/messages",
        {
            "$filter": f"receivedDateTime ge {to_graph_time(since)} and receivedDateTime lt {to_graph_time(until)}",
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,importance,webLink",
            "$top": PAGE_SIZE,
        },
    )
    items = []
    for row in rows:
        occurred = parse_iso(row.get("receivedDateTime", ""))
        if occurred is None:
            continue
        items.append(Item("mail", row["id"], occurred, row))
    return items


def collect_calendar(token: str, since: datetime, until: datetime) -> list[Item]:
    # calendarView, not /events: it expands recurring series into occurrences, which is what a
    # daily agenda needs. The window is expressed as params, not $filter.
    rows = _collect(
        token,
        f"{GRAPH}/me/calendarView",
        {
            "startDateTime": to_graph_time(since),
            "endDateTime": to_graph_time(until),
            "$orderby": "start/dateTime",
            "$select": "id,subject,start,end,location,organizer,attendees,isAllDay,isCancelled,webLink",
            "$top": PAGE_SIZE,
        },
    )
    items = []
    for row in rows:
        occurred = parse_iso((row.get("start") or {}).get("dateTime", ""))
        if occurred is None:
            continue
        items.append(Item("event", row["id"], occurred, row))
    return items


def collect_todo(token: str, since: datetime, until: datetime) -> list[Item]:
    """Tasks touched in the window, across every To Do list in this mailbox."""
    lists = _collect(token, f"{GRAPH}/me/todo/lists", {"$top": PAGE_SIZE})
    items = []
    for todo_list in lists:
        rows = _collect(
            token,
            f"{GRAPH}/me/todo/lists/{todo_list['id']}/tasks",
            {
                "$filter": f"lastModifiedDateTime ge {to_graph_time(since)}",
                "$top": PAGE_SIZE,
            },
        )
        for row in rows:
            occurred = parse_iso(row.get("lastModifiedDateTime", ""))
            if occurred is None:
                continue
            payload = {**row, "listId": todo_list["id"], "listName": todo_list.get("displayName")}
            items.append(Item("task", row["id"], occurred, payload))
    return items


def collect_chat(token: str, since: datetime, until: datetime) -> list[Item]:
    """
    Teams 1:1 and group chat messages in the window.

    The $filter/$orderby pairing below is load-bearing and silently fragile: Graph ignores a
    $filter on lastModifiedDateTime unless $orderby names the same property. Drop the $orderby and
    you get the entire chat history back with no error at all. The window is re-checked per message
    for exactly that reason.
    """
    chats = _collect(
        token,
        f"{GRAPH}/me/chats",
        {"$top": PAGE_SIZE, "$orderby": "lastMessagePreview/createdDateTime desc"},
    )

    items = []
    for chat in chats:
        rows = _collect(
            token,
            f"{GRAPH}/chats/{chat['id']}/messages",
            {
                "$filter": (
                    f"lastModifiedDateTime gt {to_graph_time(since)} "
                    f"and lastModifiedDateTime lt {to_graph_time(until)}"
                ),
                "$orderby": "lastModifiedDateTime desc",
                "$top": PAGE_SIZE,
            },
        )
        for row in rows:
            # systemEventMessage is join/leave/rename noise, never digest material.
            if row.get("messageType") != "message":
                continue
            occurred = parse_iso(row.get("lastModifiedDateTime", ""))
            # Belt and braces against the silent-filter failure above.
            if occurred is None or not (since <= occurred <= until):
                continue
            payload = {**row, "chatType": chat.get("chatType"), "chatTopic": chat.get("topic")}
            items.append(Item("chat", row["id"], occurred, payload))
    return items


COLLECTORS = {
    "mail": collect_mail,
    "calendar": collect_calendar,
    "todo": collect_todo,
    "chat": collect_chat,
}


def collect_m365(alias: str, since: datetime, until: datetime) -> CollectionResult:
    """
    All four Graph collectors for one tenant.

    Partial failure is normal and reportable: a tenant may withhold one resource (To Do disabled,
    Teams not licensed) while the rest work fine. One dead resource shouldn't discard the others.
    """
    source = f"m365_{alias}"
    try:
        token = access_token(alias)
    except GraphError as exc:
        return CollectionResult(source, "error", str(exc))

    items: list[Item] = []
    counts: list[str] = []
    failures: list[str] = []

    for name, collector in COLLECTORS.items():
        try:
            found = collector(token, since, until)
        except GraphError as exc:
            failures.append(f"{name}: {exc}")
            continue
        items.extend(found)
        counts.append(f"{name}={len(found)}")

    if failures and not counts:
        return CollectionResult(source, "error", "; ".join(failures))
    detail = ", ".join(counts)
    if failures:
        return CollectionResult(source, "partial", f"{detail} ({'; '.join(failures)})", items)
    return CollectionResult(source, "ok", detail, items)
