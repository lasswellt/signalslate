# Plan: zoom-collector

## Architecture

Extend the existing singleton `zoom` connection with `auth_mode: oauth | s2s`.

- **OAuth mode**: a user-managed Zoom General app with confidential authorization code, callback only. It reuses `pipeline/oauth_flows.py` (FlowStore, nonce cookie, callback URL builder) through a new `pipeline/oauth_zoom.py`, the Zoom counterpart of `oauth_gmail.py` plus `oauth_google.py`.
- **S2S mode**: the current `account_credentials` path, unchanged except for trimmed scopes.
- **Tokens**: in OAuth mode, `health.zoom_token()` becomes single-flight. A thread lock plus an `fcntl` lock span read → refresh → persist, because Zoom rotates refresh tokens and keeps one live pair per user and app.
- **Collector**: goes from summaries to meetings. It merges the summaries list, `previous_meetings`, and (S2S only) the `pastJoined` report by `meeting_uuid`, then enriches each meeting with the summary body, participants, and a transcript (on by default, can be switched off).
- **Digest**: a `normalize_zoom` adapter feeds triage and the digest.

**Chosen:** dual mode with OAuth as the default (research Comparison Matrix).

**Rejected:**
- S2S only: no UI sign-in, account-wide `:admin` scopes, needs admin-scope grants.
- OAuth only: breaks existing `.env` setups and loses instant and attended meeting listing, which is admin-report-only.
- Multi-account `zoom_<label>`: breaks about 20 singleton e2e assertions for an unrequested need.
- Paste-back: Zoom disallows loopback redirects for confidential clients.

## File map

| Path | Change |
|---|---|
| `pipeline/collectors/zoom.py` | ISO-Z `from`/`to` (T-001); meetings union, participants (T-006); transcript, on by default (T-007) |
| `pipeline/health.py` | trimmed `ZOOM_SCOPES` (T-001); mode-aware single-flight `zoom_token`, `ZoomAuthError`, `ZOOM_USER_SCOPES`, `check_zoom` per mode (T-005) |
| `pipeline/connections.py` | zoom `_Kind`: `auth_mode`, optional `account_id`, `refresh_token` secret, `include_transcripts`; seeding and materialize (T-002) |
| `api/routers/connections.py` | `ZoomCreate` fields (T-002) |
| `pipeline/oauth_zoom.py` (new) | authorize URL, code exchange, `refresh`, `start`, `finish_callback`; paste-back refused (T-003) |
| `pipeline/oauth_flows.py` | `PROVIDERS += "zoom"` (T-003) |
| `api/routers/oauth.py` | zoom in `_KIND_FOR_PROVIDER` and start/finish dispatch (T-004) |
| `pipeline/normalize.py` | `normalize_zoom`; Participant roles `host`/`attendee` (T-008) |
| `web/components/ConnectionDialog.vue` | Zoom auth-mode fields, redirect URL, transcripts toggle (T-009) |
| `web/pages/connections.vue`, `web/components/OAuthDialog.vue` | Zoom Sign in button, provider lookup, callback-only (T-010) |
| `tests/test_oauth_zoom.py` (new), `tests/test_collectors.py`, `tests/test_health.py`, `tests/test_connections.py`, `tests/test_api_oauth.py`, `tests/test_normalize.py`, `tests/test_management_e2e.py`, `web/tests/*.test.ts` | tests per task |
| `README.md`, `.env.example`, `docs/plans/management-ui/spec.md` | docs; reverse the "Zoom OAuth out of scope" line (T-012) |

## Coverage

| Outcome | Schema/Store | Logic | API | Page | Test |
|---|---|---|---|---|---|
| Correct summaries query + scopes | — | ✓ T-001 | — | — | ✓ T-001 |
| UI sign-in, S2S unchanged | ✓ T-002 | ✓ T-003 | ✓ T-004 | ✓ T-009, T-010 | ✓ T-003, T-004, T-011 |
| Single-flight rotating token | ✓ T-002 (secret) | ✓ T-005 | — (via Test route) | — | ✓ T-005, T-011 |
| Meetings items with summary/participants/transcript | ✓ T-002 (`include_transcripts`) | ✓ T-006, T-007 | — | ✓ T-009 (toggle) | ✓ T-006, T-007 |
| Digest-ready items | ✓ T-008 (`NormalizedItem`) | ✓ T-008 | — | — | ✓ T-008 |
| Docs | — | — | — | — | grep checks T-012 |

No empty cells remain. The live-account smoke test is manual (spec §Verification).

## Risks

- **The endpoints have never been called against a live account.** Everything comes from Zoom's OpenAPI spec. The manual smoke test in spec §Verification is a ship blocker.
- **Refresh-token loss:** a crash between Zoom's refresh response and `set_secret` loses the only valid token. T-005 keeps the critical section minimal and persists first. Recovery is signing in again.
- **90-day idle expiry:** only if the scheduler is off for 90 days. The health check surfaces it.
- **Participant role extension (T-008):** adding Literal values touches every consumer of `Participant.role` (triage prompt, tests). T-008 must grep them.
- **Task sizing:** T-001 and T-002 edit 3 files plus touch existing tests. T-006 and T-007 both edit `zoom.py`, so they run sequentially (the DAG enforces this).
- **SPIDR:** tasks are split by behaviour (auth mode, sign-in, token, enumeration, transcripts, normalize, UI), not by file. T-006 and T-007 are separate behaviours on one file.
- **Base branch:** this plan builds on unmerged management-ui work (`feat/management-ui`, 32/33 done, 1 blocked).

## Solutions consulted

- `docs/plans/BACKLOG.md`: absent.
- `docs/solutions/`: absent.
- Prior plan patterns reused: `docs/plans/management-ui/tasks.json` T-015/T-016/T-028 (verify command shapes for OAuth modules and dialogs); `pipeline/tokencache.py` (lock across load → refresh → save).

## Research

- `docs/_research/2026-09-21_zoom-collector.md` (research-critic PASS, 15/15 citations live)
- `docs/_research/2026-09-13_auth-approach.md` §2 (S2S, `:master` 403)
- `docs/_research/2026-09-21_management-ui.md` (connection store, flows, callback vs paste-back)
