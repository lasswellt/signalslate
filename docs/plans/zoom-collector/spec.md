---
status: active
priority: P1
created: 2026-09-21
ship: manual
---
# Zoom collector: meetings + AI Companion, signed in from the management UI

## Goal
Turn the summaries-only, never-live-tested Zoom collector into a meetings collector for the morning digest, and let the owner authenticate Zoom from the management UI with a browser sign-in (user-managed Zoom General app, authorization code, callback mode) while keeping the existing Server-to-Server credentials as an alternative mode. Source: `docs/_research/2026-09-21_zoom-collector.md`.

## Outcomes
- The summaries request sends `from`/`to` in Zoom's documented `yyyy-MM-dd'T'HH:mm:ss'Z'` format, and the S2S health check requires only the scopes the collector calls. → T-001
- On the Connections page a user can create a Zoom connection in "Sign in with Zoom" mode, copy the redirect URL, click Sign in, finish on Zoom and return to a connection whose health is ok. Existing `.env` S2S setups keep working unchanged. → T-002, T-003, T-004, T-009, T-010, T-011
- Concurrent token use (Test button + scheduled run + CLI) never burns the rotating refresh token: exactly one refresh happens, and the newest refresh token is persisted before use. A dead token shows "sign in again". → T-005, T-011
- A run emits one `meeting` item per meeting in the window, merged from summaries, previous scheduled meetings and (S2S only) the pastJoined report. Each item carries its summary (or `summary_available=False`), participants for hosted meetings, and a capped transcript (on by default). → T-006, T-007
- Zoom meetings normalize into `NormalizedItem`s, so triage and the digest can use them. → T-008
- README and `.env.example` document both auth modes, the scopes and the limitations. → T-012

## Out of scope
- Multiple Zoom accounts (`zoom_<label>`). The connection stays a singleton.
- Paste-back sign-in for Zoom. Zoom documents loopback redirects for PKCE/native clients only. Revisit if a secret-less public PKCE General app proves workable.
- Webhooks (`meeting.summary_completed`, deauthorization) and Marketplace publishing.
- AI Companion "My Notes", and summaries of meetings the user attended but did not host. Zoom has no API for either.
- Slack OAuth.

## Assumptions
Owner decisions (2026-09-21):
- Default auth mode is sign-in (`oauth`) for new connections. A connection seeded from `.env` with `ZOOM_ACCOUNT_ID` stays `s2s` so existing setups keep working.
- Transcripts are wanted: `include_transcripts` defaults to **on** and can be switched off. The text is capped at 200 000 chars. Meetings without cloud recording simply have no transcript and are not an error. The transcript scope is required whenever the toggle is on (user scope in `oauth` mode, `:admin` scope in `s2s` mode).
- Cross-process refresh safety uses an `fcntl.flock` lock file beside the connection DB, held together with a thread lock.

Inferred (`--autonomous`):
- `PUBLIC_BASE_URL` (https) is available for the callback. Without it, the UI explains that Zoom sign-in needs it.
- `GET /users/me` works under the listed user scopes. If Zoom requires an extra `user:read:*` scope, the T-005 scope set gets it after the live smoke test.
- Participant roles in `NormalizedItem` gain `host` and `attendee`; email roles are unchanged.
- The management-ui plan (branch `feat/management-ui`) is the base. This plan builds on its connection store, `oauth_flows`, and the Connections/OAuth dialogs.

## Verification
- `bash /home/lasswellt/.claude/plugins/cache/blitz/blitz/3.8.1/scripts/tasks.sh verify zoom-collector <id>` per task; `/blitz:check --scope plan zoom-collector` before ship.
- Manual live smoke test (no `verify[]` can capture it; the endpoints have never been called against a real account): create a user-managed General app, register `{PUBLIC_BASE_URL}/api/oauth/callback/zoom`, sign in from Connections, press Test, then run `python -m pipeline.collect zoom --raw` and confirm field names (`summaries[]`, `summary_content`, participants) match the collector's assumptions. Repeat for S2S if that mode is used.
