# SignalSlate

Every morning, one page: mail, calendar, tasks, and chat from every account you live in,
condensed into a digest and todo list, rendered to PDF, and pushed to a reMarkable tablet.
Self-hosted, LAN-only, runs anywhere Docker runs.

## Sources

| Source | What it reads | Auth |
|---|---|---|
| Microsoft 365 (any number of tenants) | Mail, calendar, Teams chat, Microsoft To Do | Delegated Graph permissions, one-time interactive sign-in per tenant |
| Zoom | Yesterday's meetings, participants, AI Companion summaries | Server-to-Server OAuth |
| Slack (any number of workspaces) | Channels, group DMs, threads since the last run | User token per workspace |
| Gmail (any number of accounts) | Mail | OAuth Desktop client, one-time interactive sign-in per account |
| reMarkable | Delivery target for the rendered PDF | One-time device pairing via `rmapi` |

Sources are declared in `.env` and toggled per-run in the web UI. Adding a tenant, workspace or
account is one more block of environment variables; nothing in the code is fixed to a count.

## Phases

1. **Auth plumbing** — every token source works and survives unattended reuse. *(code complete;
   awaiting sign-off against live tenants)*
2. **Collectors** — pull the last 24h from each source (Microsoft 365, Zoom, Slack, Gmail).
   *(built; never yet run against a live API)*
3. Synthesis — digest + todo extraction.
4. Render — PDF.
5. Deliver — push to reMarkable.

The web UI and API are already in place. A "run" today checks every active source's auth, then
collects that source's last 24h into the `collecteditem` table. Later phases extend
`pipeline/runner.py::execute_run()` without changing the API or UI.

Every collector's knowledge of its API's response shape comes from documentation, not from live
calls — see [Dry-running a collector](#dry-running-a-collector) before trusting a scheduled run.

Design decisions and the research behind them: [`docs/_research/`](docs/_research/).

## Setup

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` as you complete each app registration below.

### Microsoft 365

One multi-tenant app registration, consented separately in each tenant you use.

Azure Portal > App registrations > New registration:
- Supported account types: **Accounts in any organizational directory**
- Redirect URI: **Mobile and desktop applications** > `http://localhost`
- Authentication > **Allow public client flows: Yes**
- API permissions (delegated): `Mail.Read`, `Calendars.Read`, `Tasks.Read`, `Chat.Read`.
  Add `ChannelMessage.Read.All` only if you want Teams channel posts (needs admin consent).
  Decide the full list now; adding a scope later forces re-consent in every tenant.

Put the Application (client) ID in `M365_CLIENT_ID` and one `M365_ORG<n>_ALIAS` /
`M365_ORG<n>_TENANT_ID` pair per tenant. A tenant that insists on its own registration can set
`M365_ORG<n>_CLIENT_ID` to override the shared one.

Sign in once per tenant on a machine with a browser, then copy the cache files to the server:

```
python auth/m365_bootstrap.py <alias>
scp tokens/<alias>_cache.bin <host>:/path/to/signalslate/tokens/
```

Device code flow is deliberately not the default: Microsoft ships a managed Conditional Access
policy that blocks it, and a refresh token minted that way is revoked (`AADSTS530036`) when the
policy turns on. `auth/m365_device_auth.py` exists as a fallback for tenants that explicitly
allow it.

The daily run's silent refresh keeps the refresh token alive indefinitely. Password changes, admin
session revocation, sign-in-frequency policies, and Token Protection will kill it; the dashboard
shows the error code, and re-running the bootstrap for that alias fixes it.

If a tenant disables user consent, its admin can approve the app once with:

```
https://login.microsoftonline.com/<TENANT_ID>/v2.0/adminconsent?client_id=<CLIENT_ID>&scope=https://graph.microsoft.com/Mail.Read%20https://graph.microsoft.com/Calendars.Read%20https://graph.microsoft.com/Tasks.Read%20https://graph.microsoft.com/Chat.Read&redirect_uri=http://localhost
```

### Microsoft To Do

`MSTODO_ORG_ALIAS` names which tenant's mailbox holds your To Do lists. `Tasks.Read` is already in
the scope list above.

### Zoom

Zoom Marketplace > Develop > Build App > **Server-to-Server OAuth**, created while signed in as the
**account owner** (the scope picker only offers what the creator's role can grant). Activate the app.

| Purpose | Scope |
|---|---|
| List summaries | `meeting:read:list_summaries:admin` |
| Summary body | `meeting:read:summary:admin` |
| Past meeting details | `meeting:read:past_meeting:admin` |
| Past participants | `meeting:read:list_past_participants:admin` |
| Report: a user's past meetings | `report:read:user:admin` |
| Transcript (optional) | `cloud_recording:read:meeting_transcript:admin` |

Account side: Pro/Business+, "Meeting summary with AI Companion" enabled, and "Only share meeting
summaries by email" **off**.

Collector notes: use `GET /meetings/{uuid}/meeting_summary` (the `/accounts/...` variant is a
master-account endpoint and 403s), read `summary_content`, double-encode UUIDs containing `/`.
Enumerate past meetings via `/users/{id}/meeting_summaries` or `/report/users/{id}/meetings`, not
`/users/{id}/meetings`. Tokens last an hour with no refresh; fetch one per run.

```
python auth/zoom_s2s_auth.py
```

### Slack

Per workspace: api.slack.com/apps > Create New App > From scratch. One app per workspace,
**distribution never enabled** — internal apps keep full rate limits.

OAuth & Permissions > **User Token Scopes**:

```
channels:history  groups:history  im:history  mpim:history
channels:read     groups:read     im:read     mpim:read
users:read
```

Drop `im:history` / `im:read` if the digest shouldn't read personal DMs. Install to Workspace and
put the `xoxp-...` token in `SLACK_<LABEL>_TOKEN`; the label becomes the source id. Leave token
rotation off. No search scopes: collection is `users.conversations` →
`conversations.history?oldest=` → `conversations.replies`.

```
python auth/slack_verify.py
```

### Gmail

One OAuth Desktop client, one refresh token per account. Only `gmail.readonly` is requested;
`gmail.metadata` cannot search or read bodies, so it is not an option.

1. Google Cloud Console: create **your own** project and enable the **Gmail API** (APIs & Services >
   Library).
2. APIs & Services > OAuth consent screen: user type **External**. Add **only** the
   `https://www.googleapis.com/auth/gmail.readonly` scope.
3. Click **Publish app** so the status is **In production**. In Testing status refresh tokens expire
   after 7 days.
4. APIs & Services > Credentials > Create credentials > OAuth client ID, type **Desktop app**. Put
   its id and secret in `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`. Never commit them or share a
   client id between people. A personal-use app with fewer than 100 users is exempt from Google's
   verification; you click through the "unverified app" warning once per sign-in.
5. Sign in once per account on a machine with a browser:

   ```
   python auth/gmail_bootstrap.py <label>
   ```

   Paste the printed `GMAIL_<LABEL>_REFRESH_TOKEN=...` line into `.env` on the server; the label
   becomes the source id (`gmail_<label>`). On a headless box use `--no-browser` (and `--port` to
   pin the port): open the printed URL on any machine. The redirect goes to `127.0.0.1` on the box
   running the script, so SSH-forward that port. Every run mints a new token and Google keeps only
   100 per account per client, so do not re-run it casually.

What kills the token: a Google account password change, revoked access, six months unused, or the
consent screen left in Testing. The dashboard shows `invalid_grant`; re-running the bootstrap for
that label fixes it.

Day-8 check: after a week, confirm the Gmail health check is still green. That is the point a
Testing-status token would have died.

Workspace accounts follow the same flow. If your organization owns the project, an **Internal**
consent screen skips the unverified-app warning; an admin may still block third-party apps.

### reMarkable

Deferred to Phase 5. Uses `ddvk/rmapi` (the maintained fork). Pair once with the code from
`my.remarkable.com/device/browser/connect` — the tablet must already be linked to the account —
and keep `rmapi.conf` under `tokens/`.

### Done when

Every auth script exits 0 with no prompts on a second run. The health checks verify granted
scopes, not just token validity, so a green run means the Phase 2 collectors have what they need.

Sign-off, repeatable after any token revocation:

```
python auth/m365_bootstrap.py <alias>              # once per tenant, on a machine with a browser
scp tokens/<alias>_cache.bin <host>:/path/to/signalslate/tokens/
```

then, on the server, twice in a row — no prompts, exit 0 both times:

```
python auth/m365_bootstrap.py <alias> && python auth/zoom_s2s_auth.py && python auth/slack_verify.py
```

`m365_bootstrap.py` short-circuits to the health check and exits 0 when the cache is still good,
so it doubles as the second-run check; `python auth/gmail_bootstrap.py <label>` does the same for
Gmail. Set `SLACK_SKIP_DMS=1` in `.env` if you dropped
`im:history` / `im:read`, or the Slack check will fail on the missing scopes.

### Dry-running a collector

Exercises one source against real credentials and prints what came back. Writes nothing — no items,
no cursor advance, no run — so it's safe against production, repeatedly.

```
python -m pipeline.collect                 # list declared sources
python -m pipeline.collect slack_work      # last 24h, one line per item
python -m pipeline.collect m365_work --hours 2 --limit 3
python -m pipeline.collect gmail_personal  # last 24h of one Gmail account
python -m pipeline.collect zoom --raw      # full JSON, for checking field names
python -m pipeline.collect --all
```

Use `--raw` first on each source. The collectors parse fields named in vendor documentation
(`summary_content`, `meeting_uuid`, `lastModifiedDateTime`), and the first live response is what
confirms those names are right.

### Tests

```
pytest
```

Covers `.env` parsing, scope verification, run orchestration, and every collector's parsing and
pagination against stubbed responses. No network — which is exactly why the dry run above matters.

## Web interface

FastAPI backend + Nuxt 3/Quasar frontend in the same Docker stack. Dashboard, run history, manual
trigger, and config editor.

```
cp .env.example .env   # set LAN_HOST to the Docker host's LAN address
docker compose up -d --build
```

- Web UI: `http://<LAN_HOST>:3000`
- API: `http://<LAN_HOST>:8000/api/status`

`data/` (SQLite DB + config.json) and `tokens/` are bind-mounted so they survive rebuilds — back
both up.

### Local dev

The container runs Python 3.12 (`python:3.12-slim`), so create the venv with a 3.12 interpreter to
match; newer interpreters may lack wheels for the pinned dependencies.

```
uvicorn api.main:app --reload --port 8000      # terminal 1
cd web && npm install && NUXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev   # terminal 2
```

### Pages

- **Dashboard** (`/`) — last run status, per-source health, next scheduled run, "Run now".
- **History** (`/history`, `/history/[id]`) — every run, per-source detail, PDF download once the
  render phase exists.
- **Config** (`/config`) — schedule (cron), active sources, tracker choice. Saving reschedules
  immediately.

No auth on the interface — LAN-only by design. Revisit before exposing it anywhere else.
