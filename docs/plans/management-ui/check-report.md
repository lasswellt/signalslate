---
result: FAIL
ts: 2026-09-21T22:26:40Z
ref: 8868e477f2cd2a426807da572baa1385e38b109b
scope: plan
plan: management-ui
---

# Check — management-ui

**Result:** FAIL · **Base:** origin/main · **Files:** 71 (65 excluding lockfile/plan state) · **LOC:** +22138 / −292 (+18565 / −251 without docs)

## Summary

Every task passes its own verify[] and every gate is green, but the reject critic returned REJECT: a Gmail connection cannot be created from the UI (or the API), so the browser Gmail sign-in Outcome is unreachable. Two regressions touch installs that never set a key (M365 alias validation). Secret handling, CSRF guard, OAuth state/nonce/PKCE, redaction and crypto held up under review and 15/16 mutation probes. A human still owns the manual sign-in steps in spec §Verification (Entra custom https redirect, Google Web client, reverse-proxy cookie behavior).

## Gates

| Gate | Before fix | After fix | Status |
|---|---|---|---|
| pyright (toolchain typecheck) | 54 errors (identical to origin/main; 0 in files added by this plan; runner.py 7, db.py 6, health.py 1 pre-existing) | — | pre-existing, not attributable |
| lint | no python lint lane resolves | — | SKIPPED |
| web typecheck (nuxt typecheck) | 0 errors | — | PASS |
| tests (python, full) | 1597/1597 passed · escaped 0 | — | PASS |
| tests (web, vitest) | 128/128 passed | — | PASS |
| build (nuxt build) | PASS | — | PASS |

## Tasks (plan scope)

| Task | passes | last_verify.ok | failed | tail |
|---|---|---|---|---|
| T-001 … T-031, T-033 (32 tasks) | true | true | — | — |
| T-032 | false | blocked circuit-breaker | superseded by T-033 (malformed verify[]), Ruling in progress.md | not retried |

## Held-out checks (critic-authored, plan scope)

| Task | ok | Command | Tail |
|---|---|---|---|
| T-001 … T-018, T-020 … T-033 (31 tasks) | true | see critic output | — |
| T-019 | **false** | real app: POST /api/connections for slack, gmail, m365 with the fields the UI dialog sends | `{'gmail': 503 store_inactive (rolled back), 'm365': 201, 'slack': 201}` |

Note on T-010: its held-out probe passed but recorded that an underscore alias is now rejected (finding 2).

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| Web and API on one registrable site (Lax nonce cookie + credentials:'include') | manual run through the real reverse-proxy topology | open |
| Secure nonce cookie when UI is http but PUBLIC_BASE_URL is https | browser test on docker-compose LAN setup | open |
| window.open after await blocked by popup blockers | manual browser check | open (Reopen button exists) |
| SQLite concurrent writers (connections._write_lock vs scheduler) | engine PRAGMA read + threaded stress test | open |
| Real Google/Entra sign-in in both modes; Entra custom https redirect | spec Verification manual steps | open |
| Web component tests catch behavior regressions | mutation pass under web/ | open (only $fetch/window.open stubbed) |
| README "nothing logs a secret" | run stack, grep container logs and DB | partly answered: access log DOES log the callback query (finding 5) |

## Findings

### Critical (blocks)

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | api/routers/connections.py:260, pipeline/connections.py:683, web/components/ConnectionDialog.vue:169 | 🔴 Gmail create always answers 503 store_inactive and rolls back: materialize() emits GMAIL_<L>_REFRESH_TOKEN only when the secret exists, so known_sources() omits the id. The dialog offers gmail without a refresh_token and Sign in needs an existing connection: browser Gmail sign-in cannot start for a new account. Reproduced by me, the backend lens, the frontend lens and the critic; e2e used Slack only. Fix: make a token-less Gmail row visible to known_sources()/materialize (or create it in a pending state that the sign-in flow completes) and add a create-then-sign-in test through the real app. | critic (spec), survey:backend, survey:frontend | 0.95 |

### Major

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 2 | pipeline/tokencache.py:65, pipeline/health.py:326 | 🟡 No-key installs regress: cache_path() raises ValueError for M365 aliases outside [A-Za-z0-9-]+ (origin/main accepted any), so check_m365 crashes and the whole run fails. Guard check_m365/graph (HealthResult error) or widen to a safe filename class. | critic, survey:backend | 0.8 |
| 3 | pipeline/health.py:_live_env, pipeline/connections.py:seed_from_env | 🟡 With a key set, a legacy .env source that fails seeding (bad alias, long id) or a row that cannot decrypt silently vanishes from known_sources(): all family keys are dropped unconditionally. Keep unseeded env sources, or surface a UI/log error. | survey:backend | 0.75 |
| 4 | web/components/OAuthDialog.vue:267 | 🟡 Callback-mode poll waits on a health row/checked_at change that sign-in never writes: M365 and Gmail re-sign-in end as "expired" though the token was stored; the test fakes the health row. Poll a value the flow really changes (e.g. secrets/token presence or a server flow-status). | survey:frontend | 0.8 |
| 5 | api/Dockerfile:10, api/routers/oauth.py:321 | 🟡 uvicorn's access log records the callback query string (code and state); reproduced with a scratch app. README claims nothing logs a secret. Add --no-access-log (or a filter). Mitigated by single use, PKCE and the nonce cookie. | survey:security (reproduced) | 0.8 |
| 6 | api/security.py:156 | 🟡 No Host validation: a DNS-rebinding page can read every GET (collected items, config, status); writes stay blocked by the Origin check. README says another website cannot drive the API. Add a Host allow-list (TrustedHost from WEB_ORIGINS/LAN_HOST/PUBLIC_BASE_URL). | survey:security | 0.75 |
| 7 | pipeline/crypto.py:104, README.md:276 | 🟡 Key rotation cannot complete: Vault.rotate() is never called; rows stay under the old key until edited, so dropping the old key later makes secrets undecryptable (fails closed). Add a rotate command or startup re-encrypt and document it. | survey:security | 0.85 |
| 8 | api/routers/runs.py:17, web/pages/history.vue:46 | 🟡 /api/runs sends naive datetimes without Z and History renders them with new Date(): times are off by the viewer's UTC offset (spec assumes Z everywhere). Use iso_z in runs.py and parseUtc in history.vue. | survey:frontend | 0.7 |

### Minor

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 9 | pipeline/runner.py:303-316 | 🔵 A single-source run of an unhealthy source never calls record_attempt, so last_attempt stays stale. | survey:backend | 0.8 |
| 10 | pipeline/db.py:reset_cursor | 🔵 Reset with days_back=null deletes the whole SourceCursor row, wiping last_attempt_* with the watermark. | survey:backend | 0.7 |
| 11 | api/routers/connections.py:255-273, pipeline/config_store.py:36 | 🔵 Narrow window after create() and before set_source_active(False) where a new id defaults to ON. | survey:backend | 0.5 |
| 12 | pipeline/runner.py:381-383 | 🔵 Run.summary/error now capped at 1000 chars on no-key installs. | survey:backend | 0.5 |
| 13 | api/routers/connections.py:106, api/security.py:149 | 🔵 422 loc echoes dict KEYS of ConnectionPatch.secrets (a secret pasted as a key comes back). | survey:security | 0.8 |
| 14 | api/routers/collectors.py:229 | 🔵 Dry-run cap bypassable: a still-running job is dropped from the table after 15 min while its thread runs on. | survey:security | 0.7 |
| 15 | pipeline/oauth_flows.py:192 | 🔵 Without login, 20 POST /oauth/*/start evict the owner's pending flow; no row cap on POST /connections. | survey:security | 0.6 |
| 16 | api/routers/oauth.py:189 | 🔵 Nonce cookie Secure flag follows PUBLIC_BASE_URL, not the request scheme (UI over http with PUBLIC_BASE_URL set drops the cookie). | survey:security | 0.6 |
| 17 | api/routers/oauth.py:145 vs connections.py:155 | 🔵 InvalidConnection maps to 409 in oauth._translate but 422 in connections._translate; connection-not-found is a bare string in connections.py but a coded body in oauth.py; guard 403/415 bodies are plain strings. | survey:patterns | 0.8 |
| 18 | connections/collectors/oauth/status routers | 🔵 _coded(), _scrub() and _MAX_TEXT duplicated 3x; oauth.py imports private _out_for; main.py calls health._raw_env(); label/alias regexes live in 3 places (CLI allows uppercase Gmail labels the store rejects). | survey:patterns | 0.9 |
| 19 | api/main.py:52 vs api/routers/oauth.py:271 | 🔵 CSRF allow-list frozen at import, redirect target re-read per request; WEB_ORIGINS edits need a restart, README does not say so. | survey:patterns | 0.6 |
| 20 | web/components/ConnectionDialog.vue:158, web/pages/collectors.vue:528,779, web/pages/connections.vue:44 | 🔵 redirect_mode is stored but never used; run-poll can start after unmount; itemsMoreLoading stuck after filter change; identical toggle/button labels for screen readers. | survey:frontend | 0.7 |
| 21 | web/tests/OAuthDialog.test.ts | 🔵 q-btn--loading assertions are vacuous (Quasar never renders that class). | dev T-029 | 0.7 |
| 22 | docker-compose.yml | 🔵 NUXT_PUBLIC_API_BASE derives from LAN_HOST:8000, so a single-hostname reverse proxy setup needs an override the README does not describe. | dev T-030 | 0.7 |

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 23 | .cc-sessions/typecheck-baseline.json | ❓ startup-validate --strict exits 2 on a SCHEMA finding for a gitignored hook-owned file (not injection); caps the posture gate at CONDITIONAL. | startup-validate | 1.0 |
| 24 | docs/plans/management-ui/tasks.json | ❓ last_verify tails carry the local checkout path (Linux username = public GitHub handle); the tasks-guard hook blocks scrubbing; recorded as a Ruling. | manual | 1.0 |

## Auto-fix (`--fix`)

Not run.

## Ratchet

docs/sweeps/ratchet.json does not exist (project never onboarded); no baseline to regress against, nothing written.

| Metric | Baseline (origin/main) | Current | Threshold | Δ | Action |
|---|---|---|---|---|---|
| type_errors | 54 | 54 | project never type-clean, floor not engaged | 0 | pre-existing |
| test_count | 634 py | 1597 py + 128 web | ↑ | ↑ | — |
| mocks_in_src | 0 | 0 | 0 | 0 | — |
| todo_count | 0 | 0 | 0 | 0 | — |
| stale_worktree_branch_count | 0 | 0 | 0 | 0 | — |

## Critic

`--mode reject` verdict: REJECT · mode: in-Claude · findings: 5 (2 P0, 1 P1, 1 P2, 1 P3), all folded into findings 1-2 above.

## Security posture

registry-validate: ok · startup-validate --strict: non-zero (SCHEMA on .cc-sessions/typecheck-baseline.json, not injection) · pre-trust execution: none (one `eval` in critic-external.sh:174 is the plugin's own parameter expansion, not project content)

## Automation coverage

Deterministic gates: 5/6 (pytest, web typecheck, web tests, build, registry rows; pyright red but pre-existing; lint skipped) · ratchet not bootstrapped · critic REJECT · e2e: skipped_unavailable (no Playwright, no /verify recipe)
Not auto-verified (human owns): architectural fit, UX correctness, real Google/Entra sign-in, reverse-proxy cookie behavior, business-logic intent, regressions in untested paths.
**Recommendation:** needs-human-review

## Before merge

1. Fix finding 1: allow a token-less Gmail connection to exist (api/routers/connections.py, pipeline/connections.py:683, pipeline/health.py:249) and add a real-app create-then-sign-in test.
2. Fix finding 2: keep legacy M365 aliases working on no-key installs (pipeline/tokencache.py:65, pipeline/health.py:326); add a test with an underscore alias and no key.
3. Fix finding 3, 4 and 5 (source dropped on failed seed, callback poll detection, access log flag in api/Dockerfile).
4. Decide on 6, 7, 8 (Host allow-list, key rotation command, Z timestamps) or record each as a Ruling.
5. Run the manual steps in spec §Verification (key on/off, Gmail and Microsoft paste-back, callback mode, cross-origin write rejection).

## Later

1. Share one _coded/_scrub/error-shape helper across routers; unify label and alias validators.
2. Add a Host allow-list and flow/connection caps if the API is ever exposed beyond a trusted network.
3. Document the docker-compose single-hostname override and that WEB_ORIGINS changes need a restart.
