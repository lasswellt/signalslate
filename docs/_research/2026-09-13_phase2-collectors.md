# Phase 2 collectors: pulling the last 24h from each source

Date: 2026-09-13. Verified against primary docs on this date; unverifiable items flagged per section.

```yaml
scope:
  topic: phase2-collectors
  sources: [m365, mstodo, zoom, slack]
  depends_on: docs/_research/2026-09-13_auth-approach.md
  supersedes: README "Phases" item 2
  status: final
  capabilities:
    - 5 collectors (m365_mail, m365_calendar, m365_todo, m365_chat, slack, zoom)
    - 2 new DB tables (CollectedItem, SourceCursor)
    - 1 new module (pipeline/collectors/)
```

## Summary

Collect with **plain timestamp-filtered reads, not delta queries, and no `$batch`**. At this scale — one
user, one 24h window, a handful of tenants — delta buys nothing and costs a token store plus a `410
resyncRequired` recovery path; `$batch` buys nothing and adds a class of silent data loss where a 429
hides inside a 200 envelope. Both of the contrarian pass's top-ranked operational risks are consequences
of machinery this project does not need. Its third risk, the Slack rate-limit cap, rests on a
misclassification: Slack's own changelog exempts internal customer-built apps, which is what SignalSlate
installs.

The auth work already done is sufficient. The consented delegated scopes (`Mail.Read`, `Calendars.Read`,
`Tasks.Read`, `Chat.Read`) cover every Phase 2 endpoint, including Teams chat — no re-consent, no new
app registration, no billing configuration.

Recommendation: a `pipeline/collectors/` module of per-source functions returning a `HealthResult`-shaped
struct, called from `execute_run()` between the health check and the final commit, wrapped in per-source
`try/except`, persisting to two new tables.

## Research questions

**Q1. Delta or filtered reads?** Filtered reads. Mail/calendar/To Do/chat all support `$filter` on a
timestamp property under the scopes already held. Delta is a sync primitive for keeping a replica warm;
this is a 24h digest that re-derives its window every run.

**Q2. Does delegated `Chat.Read` actually work for Teams chat?** Yes — for the list path.
`GET /me/chats` and `GET /chats/{chat-id}/messages` both take delegated `Chat.Read` as the least-privileged
permission. Only `chatMessage: delta` is application-only. This corrects the library-docs pass, which
generalized the delta restriction to the whole resource.

**Q3. Is the Teams path metered?** No. Metering ended 2025-08-25, and it only ever applied to the
`getAllMessages` export APIs — never to per-chat message lists. SignalSlate's path was never in scope.

**Q4. Is Slack's 1 req/min cap a blocker?** No, but verify at runtime. The cap targets apps
"commercially distributed outside of the Marketplace"; internal customer-built apps keep Tier 3
(50+/min, 1000 objects). Keep distribution off and add a canary (below).

**Q5. Where does "since last run" state live?** A new `SourceCursor` DB table — not `config.json`,
which is user-editable web-UI-owned state that a `PUT /config` would clobber.

**Q6. What does the seam look like?** `pipeline/runner.py::execute_run()` between
`check_all_configured()` and the final session. No API or web-UI change needed.

## Findings

### 1. Microsoft Graph — use `$filter`, skip delta

Delta support under delegated scopes, for the record:

| Resource | Delta endpoint | Delegated? |
|---|---|---|
| `message` | `GET /me/messages/delta` | yes (Mail.Read) |
| `event` | `GET /me/events/delta`, `GET /me/calendarView/delta` | yes (Calendars.Read) |
| `todoTask` | `GET /me/todo/lists/{id}/tasks/delta` | yes (Tasks.Read) |
| `chatMessage` | `GET /chats/{id}/messages/delta` | **no — application only** |

https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0 ·
https://learn.microsoft.com/en-us/graph/delta-query-events ·
https://learn.microsoft.com/en-us/graph/api/todotask-delta?view=graph-rest-1.0

Why not use it anyway: there are two distinct ways a token stops working, and a correct implementation
handles both. A deltaToken's lifetime is "dependent on the size of the internal delta token cache" with
no fixed upper limit — older tokens are evicted as new ones are added, and an expired token returns a
40X (e.g. `syncStateNotFound`). Separately, a service-side synchronization reset — maintenance, mailbox
migration — returns `410 Gone` with `resyncRequired`, forcing a full resync. Two failure paths to write,
test, and monitor, in exchange for skipping a query the size of one day's mail.
https://learn.microsoft.com/en-us/graph/delta-query-overview

The reads to use instead:

```
GET /me/messages?$filter=receivedDateTime ge {since}&$orderby=receivedDateTime desc&$top=50
GET /me/calendarView?startDateTime={since}&endDateTime={until}
GET /me/todo/lists                       # then per list:
GET /me/todo/lists/{id}/tasks?$filter=lastModifiedDateTime ge {since}
GET /me/chats?$orderby=lastMessagePreview/createdDateTime desc   # then per chat:
GET /chats/{id}/messages?$filter=lastModifiedDateTime gt {since} and lastModifiedDateTime lt {until}
        &$orderby=lastModifiedDateTime desc&$top=50
```

Chat specifics, all from the v1.0 reference
(https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0 ·
https://learn.microsoft.com/en-us/graph/api/chat-list?view=graph-rest-1.0):

- `$filter` on `lastModifiedDateTime` supports `gt` and `lt`, **but only when `$orderby` names the same
  property** — otherwise `$filter` is silently ignored. Silent, not an error: get this wrong and the
  collector quietly returns the whole chat.
- `$top` max is 50 on both `/chats` and `/chats/{id}/messages`.
- `/me/chats` covers `oneOnOne`, `group`, and `meeting` chat types — filter on `chatType` if meeting
  chats are noise.
- Filter out `messageType != "message"` to drop `systemEventMessage` join/leave/rename noise.

Pagination is uniform: follow `@odata.nextLink` until absent.

### 2. Microsoft Graph — no `$batch`

Individual requests inside a `$batch` are throttled individually and a 429 is reported *inside* the
per-request status, while the envelope returns `200 OK`. Code that checks only the outer status drops
those items silently. https://learn.microsoft.com/en-us/graph/throttling

For one user's daily pull, sequential requests with `timeout=15` are well inside any published limit.
Adding `$batch` here trades a non-problem (request count) for a silent-data-loss failure mode. Revisit
only if tenant count grows enough to make wall-clock a real complaint.

Per-service throttling limits for Outlook/To Do/Teams are not enumerated on the throttling-limits page
(`UNVERIFIED` — the page documents the 429 + `Retry-After` contract, not per-resource numbers). Honor
`Retry-After` and move on.

### 3. Slack — exempt, but prove it at runtime

The 2025-05-29 change caps `conversations.history`/`.replies` at 1 req/min and 15 objects for apps
"commercially distributed outside of the Marketplace (also called 'unlisted' apps)". The same changelog
states verbatim: **"Internal customer-built applications are not impacted by these changes."** The
2025-06-03 clarification repeats it: "Any internal customer-built apps will maintain their existing rate
limits and will not be subject to the new posted limits."
https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/ ·
https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/

Slack does not publish the mechanism that classifies an app as internal (carried forward as a flag from
the auth research). So do not merely assume the exemption — detect it:

> **Canary:** request `conversations.history` with `limit=200`. Getting exactly 15 objects back with
> `has_more: true` means this app has been classified as capped. Surface that as a `SourceHealth` error
> with the remedy ("app has been rate-limit classified; check distribution settings") rather than
> silently collecting 7% of the day.

Collection sequence, unchanged from the auth research: `users.conversations` → per channel
`conversations.history?oldest={ts}` → per parent with `reply_count > 0`, `conversations.replies?ts={thread_ts}`.

- Timestamps are Unix seconds as strings (`"1234567890.123456"`), not ISO 8601. `oldest` is exclusive
  unless `inclusive=true`.
- Cursor pagination: `response_metadata.next_cursor`, empty when exhausted. Slack recommends ≤200 per page.
- Thread discovery: a parent carries `thread_ts` and `reply_count`; only call `conversations.replies`
  when `reply_count > 0`. (`UNVERIFIED` as an explicit doc statement — it is the documented message
  shape, not a documented traversal recipe.)
- Filter the `USLACK` system user (per the auth research, system notifications moved to it 2026-06-17).
https://docs.slack.dev/reference/methods/conversations.history/ · https://docs.slack.dev/apis/web-api/rate-limits

### 4. Zoom — carried forward, verify empirically

The auth research verified the endpoint pair and scopes against the embedded OpenAPI and Zoom staff
forum guidance; this pass could not independently re-fetch them (developers.zoom.us renders its API
reference client-side, so both retrieval agents came back empty and one wrongly concluded no public
summary API exists). A devforum thread discusses `GET /v2/meetings/meeting_summaries` as the account-level
listing endpoint, though that thread's own outcome is unresolved — supporting context, not confirmation.
https://devforum.zoom.us/t/is-there-an-api-endpoint-to-list-and-retrieve-ai-companion-notes-across-an-account/144953

**Verify these two endpoints empirically before building the Zoom collector** — a live call against the
account is the only evidence that settles it.

Treat as carried forward from the auth doc, not re-verified here:

```
GET /users/{userId}/meeting_summaries?from={date}&to={date}
        &time_filter_field=summary_created_time&page_size=300
GET /meetings/{meetingUUID}/meeting_summary      # non-master path
```

Parse `summary_content` (Markdown). Double-encode any UUID starting with `/` or containing `//`. Fetch a
fresh token per run — Zoom S2S tokens last an hour with no refresh.

**Summaries arrive late.** They are generated after the meeting ends, and the forum records cases of
summaries that never generate at all ("insufficient transcript"). A same-day window will miss meetings
that ended near the boundary. Use a **lookback wider than the digest window** — query 48h, digest 24h,
dedupe on meeting UUID — so a late summary lands in the next day's digest instead of vanishing.

### 5. The seam in this codebase

`execute_run()` (`pipeline/runner.py:17-53`) is the only entry point for both the scheduler
(`api/scheduler.py:15-16`) and the manual trigger (`api/routers/runs.py:44-49`). Collectors go between
`check_all_configured()` (`runner.py:26`) and the final session block (`runner.py:31`).

The web UI needs **zero changes** if collectors keep populating `Run.summary`, `Run.status`, and
`SourceHealth` rows — `web/pages/index.vue` and `web/pages/history/[id].vue` render only
`source`/`status`/`detail`/`checked_at` plus the run's summary and error strings. Item-level browsing
would need new UI; defer it.

Reuse verbatim, do not reimplement (all in `pipeline/health.py`):

| Function | Line | Purpose |
|---|---|---|
| `m365_aliases()`, `slack_workspaces()`, `m365_tenant_config()` | `:63-101` | the only `.env` source of truth |
| `known_sources()` | `:207-209` | source ids that key `active_sources` |
| `m365_cache_path()`, `m365_app()` | `:104-114` | MSAL cache path + per-tenant app |
| `check_m365`'s silent-token pattern | `:117-146` | `acquire_token_silent_with_error` + cache rewrite; take `result["access_token"]` |
| `check_zoom`'s token fetch | `:157-176` | factor out the token half for collector reuse |

`HealthResult` (`health.py:48-52`) is the shape to mirror. Timestamps are **naive UTC via
`datetime.utcnow()`** everywhere (`db.py:21`, `db.py:34`, `runner.py:36`) — collectors computing "last
24h" must match, not `datetime.now(timezone.utc)`.

### 6. State: two new tables

Nothing persists a watermark today — not in `pipeline/db.py`, not in `config.json`
(`config_store.py:16-27` is `schedule_cron`, `tracker`, and a flat `active_sources` bool map).

```python
class CollectedItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id")
    source: str                    # m365_<alias> | slack_<label> | zoom
    item_type: str                 # mail | event | task | chat | message | meeting
    external_id: str               # for dedupe across overlapping windows
    occurred_at: datetime          # the item's own timestamp, naive UTC
    payload: str                   # JSON blob; normalize in Phase 3, not here

class SourceCursor(SQLModel, table=True):
    source: str = Field(primary_key=True)
    last_success_at: Optional[datetime] = None
    cursor: Optional[str] = None   # unused while on filtered reads; present for later
```

`SourceCursor` lives in the DB, not `config.json`, because `config.json` is web-UI-owned and a `PUT
/config` full-replace would clobber pipeline-internal state. Keep it independent of `run_id` so a failed
run does not lose the watermark.

Collect on `max(last_success_at - overlap, now - 24h)` rather than a naive 24h window, so a missed run
backfills instead of silently dropping its window. Dedupe on `(source, external_id)`.

## Risks

**Silent `$filter` failure on Teams chat.** If `$orderby` does not name the same property as `$filter`,
Graph ignores the filter and returns the entire chat history rather than erroring. A collector that looks
like it works can be pulling everything. Mitigate with an assertion on the returned window and a test
that pins the exact query string.

**Slack classification is undetectable in advance.** Slack publishes the exemption but not the mechanism
that grants it, so the only honest posture is to keep distribution off and detect the capped state at
runtime via the canary above. If the canary ever fires, the digest is silently incomplete until someone
looks.

**Zoom summaries are not guaranteed to exist.** A meeting can end with no summary — permanently, not just
late. The collector should record the meeting with an explicit "no summary available" marker rather than
omitting it, so the digest can say so instead of silently under-reporting the day.

**`execute_run()` has no error isolation today** (`runner.py:26-52`). An exception anywhere leaves the
`Run` row stuck at `status="running"` forever, because the final session block that sets a terminal
status is never reached. Phase 2 multiplies the failure surface per source by an order of magnitude, so
per-source `try/except` is a prerequisite, not a nicety — and the terminal-status write belongs in a
`finally`.

**Concurrent runs are unguarded.** The manual trigger (FastAPI `BackgroundTasks`) and the scheduler
(APScheduler thread) can both enter `execute_run()` at once; nothing prevents it, and SQLite has no WAL
mode or retry configured. Health checks are fast enough that the window is currently theoretical.
Minutes-long collection makes it real. Add a guard that refuses to start when a `status="running"` Run
exists, and consider `PRAGMA journal_mode=WAL`.

**Long runs occupy an APScheduler worker thread.** `_run_scheduled` calls `execute_run` synchronously on
the scheduler's small default thread pool, with no timeout or watchdog. A hung collector holds that
thread indefinitely; per-request `timeout=15` bounds each call but not the run as a whole.

## Open questions

- Whether per-source settings (lookback window, channel/folder selection, DM inclusion) should move into
  `config.json`. Doing so is not a drop-in: `config_store._defaults()`/`_merge()`, `ConfigUpdate`
  (`api/routers/config.py:10-13`), and `DigestConfig` (`web/composables/useApi.ts:37-41`) are a flat
  three-field shape that must change in lockstep. `SLACK_SKIP_DMS` is currently an `.env` flag, which is
  already inconsistent with a per-workspace UI toggle.
- Whether `ChannelMessage.Read.All` (Teams *channel* posts, admin consent) is wanted. Out of scope while
  the scope list stays as consented.
- Exact per-resource Graph throttling numbers — not published; behavior-only contract.

## Implementation sketch

1. `pipeline/collectors/__init__.py` — `CollectionResult` mirroring `HealthResult`, plus `collect_all(active_sources, since, until)`.
2. `pipeline/collectors/graph.py` — one shared `_graph_get(token, url, params)` with `@odata.nextLink` paging, `timeout=15`, `Retry-After` honoring; then `collect_mail`, `collect_calendar`, `collect_todo`, `collect_chat` per alias, taking the token from `health.py`'s silent-refresh pattern.
3. `pipeline/collectors/slack_.py` — `users.conversations` → `conversations.history` → `conversations.replies`, cursor paging, `USLACK` filter, rate-limit canary.
4. `pipeline/collectors/zoom_.py` — reuse the S2S token fetch, `meeting_summaries` enumeration over a 48h lookback, per-UUID summary fetch with double-encoding.
5. `pipeline/db.py` — add `CollectedItem` + `SourceCursor` and their query helpers alongside `source_health_for_run`/`latest_source_health`.
6. `pipeline/runner.py` — call collectors per active source inside `try/except`, write items + cursors, move terminal-status assignment into `finally`, add the already-running guard, and extend `run.summary` to counts.
7. `tests/test_collectors.py` — follow `tests/test_health.py`: the `env` fixture monkeypatching module-level `ROOT`/`TOKEN_DIR`, a `FakeResponse` extended for `@odata.nextLink` and Slack cursors, `monkeypatch.setattr(module.requests, ...)`. Add `tests/test_runner.py` for `execute_run` against a temp DB — none exists today.

## References

- https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/chat-list?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/todotask-delta?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/delta-query-overview
- https://learn.microsoft.com/en-us/graph/delta-query-events
- https://learn.microsoft.com/en-us/graph/throttling
- https://learn.microsoft.com/en-us/graph/teams-licenses
- https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/
- https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/
- https://docs.slack.dev/reference/methods/conversations.history/
- https://docs.slack.dev/apis/web-api/rate-limits
- https://devforum.zoom.us/t/is-there-an-api-endpoint-to-list-and-retrieve-ai-companion-notes-across-an-account/144953
- https://devforum.zoom.us/t/cannot-retrieve-ai-companion-meeting-summary-body-via-api-missing-meetingsummary-master-scope/142967
