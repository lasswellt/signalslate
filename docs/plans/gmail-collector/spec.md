---
status: active
priority: P1
created: 2026-09-20
ship: manual
---
# Gmail collector and the first Phase 3 (agents) slice

## Goal
Add Gmail as a collected source, one Google account per label, alongside Microsoft 365, Slack and
Zoom, then take the first slice of Phase 3: a source-agnostic `NormalizedItem` with a Gmail adapter
and a single structured-output "map" (triage) call over collected Gmail items. Stops before the
reduce step. Grounded in `docs/_research/2026-09-20_gmail-collector-and-agents.md`.

## Outcomes
- A declared Gmail account shows up as `gmail_<label>` everywhere sources are listed (dashboard,
  config toggles, dry-run CLI, runs) and its health check reports token validity and granted scope;
  a dead token says why and how to fix it.  → T-002, T-003, T-006
- `python auth/gmail_bootstrap.py <label>` signs in once on a machine with a browser and prints the
  refresh token to paste into `.env`, refusing a token that lacks `gmail.readonly`.  → T-007
- `python -m pipeline.collect gmail_<label> --raw` returns the last 24h of mail as `mail` items with
  a flattened payload (decoded body, headers, labels, thread id), and a scheduled run persists and
  dedupes them like any other source.  → T-004, T-005, T-006
- The collector cannot silently under-collect or blow quota: epoch-second window (never date
  strings), every page drained, `MAX_PAGES` raises rather than truncates, paced sequential gets,
  backoff on 403-rate/429/5xx, no batch/history/watch.  → T-004
- Collected Gmail items normalize to `NormalizedItem`, and a map call turns them into validated
  triage records with provenance (per-run alias to real id), falling back to deterministic stubs
  when the model call fails, so a bad call yields a partial digest and not a dead run.
  → T-009, T-010, T-012, T-014
- Hostile email cannot steer the summarizer: no tools, JSON-encoded untrusted content, hidden HTML
  removed, model text sanitized, all pinned by regression tests.  → T-005, T-012, T-013
- Setup is documented for a public repo (own GCP project, publish to "In production", unverified
  click-through, password-change warning) with generic wording only.  → T-008, T-011

## Out of scope
- IMAP + app-password fallback (documented in the research as the fallback if the day-8 token check
  fails; not built).
- Google Workspace domain-wide delegation; Workspace works through the same Desktop client flow,
  with an "Internal" consent screen if the org allows it.
- The reduce call, digest and todo rendering, PDF, reMarkable delivery (Phases 3-5 proper).
- Gmail permalinks: `NormalizedItem.permalink` exists but stays `None`; a URL format that works
  across accounts is unverified and belongs to the render phase.
- Batch API / Message Batches, prompt caching, Managed Agents, Agent SDK.
- Web UI changes: sources flow through `known_sources()` into config and dashboard already.
- Category/label filtering at query time; Phase 3 decides from `labelIds` in the payload.

## Assumptions
- Consumer @gmail.com is the primary target; Workspace uses the same flow.
- Collect all non-spam, non-trash mail including SENT, tagged by `labelIds`. Filtering is deferred
  until real volume is visible.
- The refresh token lives in `.env` (`GMAIL_<LABEL>_REFRESH_TOKEN`), Slack-style. Google returns no
  new refresh token on refresh, so there is no cache to write back.
- One shared `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET` (the user's own GCP project) serves every
  label; a Desktop client can authorize any number of Google accounts.
- Plain `requests.get`/`post`, no Google client library, no `requests.Session`, no parallelism.
  This departs from the research sketch (keep-alive session) to keep the existing `route()` test
  helper valid; ~300 sequential calls do not need connection reuse.
- `BODY_CAP` defaults to 20,000 characters. This is a guess to be re-tuned from the dry run.
- Map model `claude-haiku-4-5-20251001` by default, overridable by env; the reduce model is not
  chosen here.
- The plan was scoped with the user (2026-09-20) as "Collector + Phase 3 slice".

## Verification
- Per task: `tasks.sh verify gmail-collector <id>` (the blitz plugin's `scripts/tasks.sh`)
- Whole plan before ship: `/blitz:check --scope plan gmail-collector`
- **Manual, before T-002 is worth trusting (Step 0):** create your own GCP project, enable the Gmail
  API, configure the consent screen as External with `gmail.readonly`, **Publish app** to In
  production, create a Desktop client, and record the date you first authorize. Testing status
  means 7-day refresh tokens.
- **Manual, day 8 or later:** confirm the refresh token still works. The docs say only Testing
  expires, but one forum report says a production-status unverified app still expired. If it does,
  stop and reconsider the IMAP + app-password fallback before building further.
- **Manual, after T-008:** `python -m pipeline.collect gmail_<label> --raw --hours 2 --limit 3`
  against a real account. Settle what the docs leave open: `after:`/`before:` boundary behavior,
  list ordering, drafts, whether `metadata` returns `snippet`, real payload sizes (re-tune
  `BODY_CAP`). Recommended checkpoint: do this before starting T-009 so the adapter is designed
  against real data.
- **Manual, after T-014:** `python -m pipeline.triage_cli gmail_<label> --dry` to inspect the
  request, then one live run (spends API credit) to judge triage quality.
