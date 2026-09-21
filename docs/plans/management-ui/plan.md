# Plan: management-ui

Class: architectural. Research: `docs/_research/2026-09-21_management-ui.md` (three parallel research agents: OAuth redirect rules, library and stack choices, codebase seam map; consolidated there).

## Architecture

**Store and seam.** A `connection` table plus a `tombstone` table hold connections; secrets are one encrypted JSON envelope per row (Fernet/MultiFernet, key from `SIGNALSLATE_SECRET_KEY`), decrypted only in `pipeline/connections.py`. `pipeline/health.py:_env()` stays the single choke point every collector already reads: when a store is active it removes the connection-owned key families from the raw environment and adds the keys materialized from the store, using the env-style names the collectors already parse. No collector changes. `.env` seeds the store once per connection id at startup (tombstones stop deletions from being re-seeded; existing rows are never overwritten: store wins). With no key the overlay is off and everything behaves as today.

**Run consistency.** `execute_run` freezes an env snapshot (contextvars) for the whole run, so a UI edit mid-run cannot make `known_sources()` and `dispatch()` disagree; a lock closes the check-then-insert race between scheduler and UI triggers; `execute_run(only=source)` powers "run one collector"; a per-source last-attempt record feeds the UI.

**Sign-in.** Two modes over one server-side pending-flow store (state, PKCE verifier and provider flow dict never leave the server; single use; 15 minute TTL; bound to the initiating browser by an HttpOnly SameSite=Lax nonce cookie). Paste-back parses a pasted loopback URL (never fetches it). Callback uses `PUBLIC_BASE_URL` and a fixed post-callback redirect. Google uses the existing PKCE helpers moved out of the CLI; Microsoft uses MSAL's `initiate_auth_code_flow` and `acquire_token_by_auth_code_flow`, writing the same per-alias cache file the CLI writes (now with a lock and atomic 0600 writes).

**API safety (no login).** Secrets are write-only (explicit response models, `SecretStr` inputs, a 422 handler that drops `input`), all persisted and returned text goes through one `redact()`, CORS is an explicit allow-list, and every non-safe method needs a custom header plus a matching Origin (CORS alone does not stop the write).

**Web.** Existing Nuxt/Quasar app gains a typed API client, a Connections page (overview, add/edit/delete dialog, sign-in dialog) and a Collectors page (run, dry run, reset, items). The web app gets typecheck and component tests in this plan.

**Rejected.**
- *Refactor every consumer onto a provider interface:* touches health, dispatch, collectors and tests for no gain over the overlay.
- *Generated key file in `data/`:* copied by the same backup as the database.
- *TypeDecorator column encryption:* implicit decrypt on every ORM load leaks plaintext into list endpoints and makes a wrong key break plain SELECTs.
- *App login / admin token:* declined by the owner; mitigated with write-only secrets, redaction and the CSRF guard.
- *Persisting pending flows in the database:* extra table and cleanup for a 15-minute lifetime; in-memory is enough for one process.
- *A second "enabled" flag on connections:* `active_sources` stays the single toggle.

## Contracts

Ids: `m365_<alias>`, `zoom`, `slack_<label>`, `gmail_<label>`. Settings: `SIGNALSLATE_SECRET_KEY`, `WEB_ORIGINS`, `PUBLIC_BASE_URL`.

Materialized keys: slack `SLACK_<L>_TOKEN`; gmail `GMAIL_<L>_REFRESH_TOKEN`, `GMAIL_<L>_CLIENT_ID`, `GMAIL_<L>_CLIENT_SECRET`; m365 `M365_ORG<n>_ALIAS/TENANT_ID/CLIENT_ID`; zoom `ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET`.

Routes (all under `/api`):

| Route | Purpose |
|---|---|
| `GET /system` | key configured, store active, callback available, allowed origins, oauth modes per provider |
| `GET /connections` | list views (no secrets, `secrets_set` names, health, active) |
| `POST /connections` | create (inactive), 409 duplicate, 503 `secret_key_missing` |
| `PATCH /connections/{id}` | replace config and only the secrets provided |
| `DELETE /connections/{id}` | delete, tombstone, forget toggle and watermark (items kept) |
| `POST /connections/{id}/test` | run the health check, redacted result |
| `GET /collectors` | per source: active, watermark, streak, threshold, last attempt, item count |
| `POST /collectors/{source}/run` | run one source (202, 409 if a run is active) |
| `POST /collectors/{source}/dry-run`, `GET /collectors/dry-run/{job_id}` | preview job |
| `POST /collectors/{source}/reset`, `.../clear-failures` | watermark and streak |
| `GET /collectors/{source}/items`, `.../items/{id}` | keyset paged items, bounded payload |
| `POST /oauth/{provider}/start`, `POST /oauth/{provider}/paste`, `GET /oauth/callback/{provider}` | sign-in (provider google or microsoft) |

## File map

| Path | Change | Tasks |
|---|---|---|
| `requirements.txt` | pin `cryptography`, `httpx` | T-001 |
| `pipeline/crypto.py` (new) | Vault, key handling, genkey CLI | T-002 |
| `pipeline/redact.py` (new) | one redaction function | T-003 |
| `pipeline/db.py` | Connection, Tombstone, SourceCursor last-attempt columns, collector helpers | T-004, T-005 |
| `pipeline/connections.py` (new) | validated CRUD, seed, materialize, overlay provider | T-006, T-007 |
| `pipeline/health.py` | overlay seam, snapshot/override, settings, per-label Gmail creds, tokencache use | T-008, T-010 |
| `tests/conftest.py` | new setting keys | T-008 |
| `pipeline/tokencache.py` (new), `pipeline/collectors/graph.py` | per-alias lock, atomic 0600 cache writes | T-009, T-010 |
| `pipeline/runner.py`, `pipeline/config_store.py` | `only=`, run lock, snapshot, last attempt, redaction, toggles | T-011, T-012 |
| `pipeline/collect.py` | pure `dry_run()` | T-013 |
| `pipeline/oauth_google.py` (new), `auth/gmail_bootstrap.py` | pure helpers moved, CLI re-exports | T-014 |
| `pipeline/oauth_flows.py`, `oauth_gmail.py`, `oauth_m365.py` (new) | pending store and paste-back, Google flow, Microsoft flow | T-015, T-016, T-017 |
| `api/security.py` (new), `api/main.py` | CORS allow-list, CSRF guard, non-echoing 422; routers, seed, overlay wiring | T-018, T-022 |
| `api/serialize.py`, `api/routers/connections.py` (new) | Z timestamps, connection routes | T-019 |
| `api/routers/collectors.py`, `api/routers/oauth.py` (new), `api/routers/status.py` | collector and sign-in routes, `/system`, redacted `/status` | T-020, T-021, T-022 |
| `web/package.json`, lockfile, `tsconfig.json`, `vitest.config.ts` | typecheck and test harness | T-023, T-024 |
| `web/composables/useApi.ts`, `web/nuxt.config.ts` | typed client, CSRF header, Dialog plugin, icon key fix | T-025 |
| `web/pages/connections.vue`, `web/app.vue`, `web/components/ConnectionDialog.vue`, `OAuthDialog.vue` | overview, edit, sign-in | T-026, T-027, T-028 |
| `web/pages/collectors.vue` | collector controls | T-029 |
| `README.md`, `.env.example` | security model, setup, registration steps | T-030 |
| `tests/*` (many new), `web/tests/*` | unit, component and end-to-end tests | T-002 to T-031 |

## Coverage

| Outcome | Crypto / redact | DB | Connection service | Env overlay | Runner | API | Web | Test |
|---|---|---|---|---|---|---|---|---|
| Overview + test | ✓ T-003 | ✓ T-004 | ✓ T-006 | ✓ T-008 | — | ✓ T-019 | ✓ T-026 | ✓ T-019, T-026 |
| Edit credentials, no restart | ✓ T-002 | ✓ T-004 | ✓ T-006, T-007 | ✓ T-008 | — | ✓ T-019, T-022 | ✓ T-027 | ✓ T-031 |
| Browser sign-in (both modes) | — | — | ✓ T-006 | — | — | ✓ T-021 | ✓ T-028 | ✓ T-015..T-017 |
| Collector controls | ✓ T-003 | ✓ T-005 | — | — | ✓ T-011, T-013 | ✓ T-020 | ✓ T-029 | ✓ T-020, T-029 |
| Safe with no login | ✓ T-002, T-003 | — | ✓ T-006 | ✓ T-008 | ✓ T-012 | ✓ T-018 | ✓ T-025 | ✓ T-031 |
| Existing installs unchanged, documented | — | ✓ T-004 | ✓ T-007 | ✓ T-008 | ✓ T-011 | ✓ T-022 | ✓ T-023, T-024 | ✓ T-030, T-031 |

No gaps. Parallel layers (by dependency): T-001 and T-023 first; then T-002, T-003, T-004, T-009, T-013, T-014, T-015, T-024 together; the chains on `pipeline/db.py`, `pipeline/connections.py`, `pipeline/health.py`, `pipeline/runner.py`, `api/main.py`, `web/pages/connections.vue` and `web/app.vue` are ordered by dependency (checked: no two unordered tasks share a file).

## Risks

- **Unverified provider details:** whether Entra's portal accepts an arbitrary https custom redirect under "Mobile and desktop applications"; any-port loopback matching on a Google Web client; whether `extras.icons` was silently ignored (the rename to `fontIcons` needs a visual check). The manual sign-in step settles the first.
- **No app login.** Anyone who can reach the API can make the app use its secrets; encryption at rest only protects copies of `data/`. Mitigated (write-only responses, redaction, origin/header guard) but not removed; README states it plainly. A future plan can add an admin token cheaply because every route already passes through one guard.
- **Losing `SIGNALSLATE_SECRET_KEY` loses every stored secret;** `.env` can re-seed only ids that were never stored. Documented; rotation via a comma-separated list.
- **Store wins is disruptive by design:** editing `.env` for an existing id silently stops mattering, and a deleted seeded id never returns. The UI and README say so.
- **Concurrency:** UI edits versus the scheduler thread (snapshot per run), UI run versus scheduled run (lock), MSAL cache read-modify-write (per-alias lock plus atomic write). `_persist` still relies on runs not overlapping and there is no unique constraint on `(source, external_id)`: a single-source run goes through the same guard, no constraint is added here.
- **Deadlock hazard:** `config_store.load_config` holds a lock while calling `known_sources()`; the store must never call it (called out in T-006 and T-011).
- **Redaction is best effort:** pattern-based plus the current secret values; an unknown token shape in an upstream error body could pass; capped lengths and class-name-only failures limit exposure.
- **Microsoft codes live about a minute:** paste-back can fail if the user is slow; the UI shows a countdown and the flow can simply be restarted.
- **Size:** 31 tasks. The tasks are cut by behavior and layer, not by file. Early layers are independent, so a wave build is possible after review.
- **Frontend tooling:** `typescript` 7.x breaks vue-tsc (pin `~6.0.x`); `web/node_modules` must exist for every web task's verify (T-023 leaves it in place); the web Dockerfile's Node version was not checked.
- **Public repo hygiene:** no real hostname, IP or tenant anywhere; T-030 carries a grep guard for home paths and private IPs, and hostnames are written as `<your-host>`.

## Solutions consulted

None: `docs/solutions/` and `docs/plans/BACKLOG.md` do not exist in this repo.

## Research

- `docs/_research/2026-09-21_management-ui.md`
- Earlier context: `docs/_research/2026-09-13_auth-approach.md`, `docs/_research/2026-09-20_gmail-collector-and-agents.md`
