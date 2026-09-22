---
status: active
priority: P1
created: 2026-09-21
ship: manual
---
# Management web interface: connections, credentials, browser sign-in and collector controls

## Goal
Let the owner manage everything the app collects from, in the browser: see every connection's health, add, edit and remove connections and their credentials (stored encrypted and never shown again), sign in to Gmail and Microsoft 365 from the browser, and control each collector (run it, preview it, see and reset its watermark, browse what it collected). Today all of this needs `.env` edits, a container restart and CLI scripts run on a laptop. Grounded in `docs/_research/2026-09-21_management-ui.md`.

## Outcomes
- The Connections page lists every M365 tenant, Zoom account, Slack workspace and Gmail account with live health, origin (env or ui), the NAMES of secrets that are set, an active toggle, and a Test button that runs that connection's health check and shows a redacted result.  → T-019, T-026, T-008, T-010
- A connection can be created, edited and deleted from forms with no `.env` edit and no restart; secrets are write-only (no API response ever contains one), encrypted at rest under `SIGNALSLATE_SECRET_KEY`, and the UI store wins over `.env` (`.env` only seeds it once per connection id; a deleted id stays deleted).  → T-002, T-004, T-006, T-007, T-008, T-019, T-022, T-027
- Gmail and Microsoft sign-in completes from the browser in two modes: paste-back (works with today's client registrations) and an automatic HTTPS callback (offered only when a public HTTPS base URL is configured), each storing its token and updating the connection.  → T-014, T-015, T-016, T-017, T-021, T-028, T-009, T-010
- Each collector can be run on its own, dry-run previewed, have its watermark and failure streak inspected and reset, and its collected items browsed, with the last result recorded per source.  → T-005, T-011, T-013, T-020, T-029
- The management surface is safe without an app login: secrets never leave the server, error and status text is redacted, mutating routes reject cross-origin and header-less requests, and validation errors do not echo submitted values.  → T-003, T-012, T-018, T-025, T-031
- Existing installs keep working untouched: with no key configured the app behaves exactly as today, and setup, security model and sign-in registration steps are documented for a public repo.  → T-001, T-023, T-024, T-030

## Out of scope
- App login, user accounts, roles, or password reset (owner chose network or reverse-proxy protection).
- Connections that are not M365, Zoom, Slack or Gmail (Todoist, reMarkable, Anthropic key management); Slack OAuth (it uses a user token); Zoom OAuth moved to docs/plans/zoom-collector.
- TLS, DNS, reverse-proxy or hostname setup (documented only; the owner's real hostname must never appear in a tracked file).
- Phases 3 to 5 of the digest (synthesis, render, deliver) and any change to collector behavior.
- Persisting pending sign-in flows across a restart (they live 15 minutes in memory), multi-process deployment, and encrypting the MSAL cache files (they stay 0600 files).
- A generated-key-file fallback for encryption (deliberately omitted, see Assumptions).

## Assumptions
- Encryption key comes only from `SIGNALSLATE_SECRET_KEY` (comma-separated, first is primary) and the store fails closed when it is unset: the app then runs on `.env` exactly as today and the UI shows a banner. This replaces the "else a generated key file" wording of the earlier design summary: a key file beside the database is copied by the same backup as the data and protects almost nothing (research section 7).
- Connection ids equal source ids (`m365_<alias>`, `zoom`, `slack_<label>`, `gmail_<label>`); labels are `[a-z0-9]+`, M365 aliases `[A-Za-z0-9-]+`; Zoom is a singleton; each Gmail connection carries its own client id and secret (a Web client for callback mode differs from a Desktop client).
- The store overlays `pipeline/health.py:_env()` using the env-style key names the collectors already parse, so no collector is rewritten; the family keys are removed from the raw environment when the store is active (otherwise deleted connections would resurrect from the container environment).
- New connections are created inactive until tested and toggled on.
- CORS allows only `WEB_ORIGINS` (default `http://localhost:3000`, plus `http://<LAN_HOST>:3000` when `LAN_HOST` is set); every non-safe request must carry `X-Requested-With: signalslate`; bodies must be JSON.
- The OAuth callback URL is built from `PUBLIC_BASE_URL` (https only), never from the request Host header; the flow is bound to the initiating browser by an HttpOnly SameSite=Lax nonce cookie, single use, 15-minute TTL.
- Microsoft paste-back keeps the existing `http://localhost` redirect; Gmail paste-back uses a loopback URI with a fixed documented port; callback mode needs a Google Web-application client and a custom https redirect on the same Entra registration under "Mobile and desktop applications".
- Timestamps leave the API with a `Z` suffix (naive UTC today would be parsed as local time by the browser).
- The web app gets a real verify story in this plan (typescript ~6.0.x, vue-tsc, vitest, happy-dom); two pre-existing typecheck errors are fixed as part of it.
- A public hostname exists for the owner's server; it is used only in private setup, never in this repo.

## Verification
- Per task: `tasks.sh verify management-ui <id>` (the blitz plugin's `scripts/tasks.sh`); whole plan before ship: `/blitz:check --scope plan management-ui`.
- **Manual, first:** generate a key (`python -m pipeline.crypto genkey`), put it in `.env`, start the stack, and confirm the seeded connections appear with origin env and the banner is gone; unset it and confirm env-only behavior returns.
- **Manual, sign-in:** Gmail paste-back with the existing Desktop client; Microsoft paste-back inside a minute; then register the https callback (Google Web client redirect, Entra custom redirect under Mobile and desktop applications) and confirm the automatic flow, which also settles the open portal question in the research doc.
- **Manual, safety:** from a browser tab on another origin, try a JSON POST to a mutating route and confirm it is rejected; confirm no response, log line or database row contains a secret (`data/digest.db`, container logs).
