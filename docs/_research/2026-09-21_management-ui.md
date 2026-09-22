# Management UI: connections, credentials, sign-in and collector controls

Date: 2026-09-21. Primary docs fetched this date; anything not confirmed verbatim is marked `UNVERIFIED`. Design proposals are marked `PROPOSAL`.

```yaml
scope:
  topic: management-ui
  depends_on:
    - docs/_research/2026-09-13_auth-approach.md
    - docs/_research/2026-09-20_gmail-collector-and-agents.md
  status: final
  decisions_from_owner:
    - scope: full (monitor + control, edit credentials, browser OAuth sign-in, collector controls)
    - access: no app login (network / reverse proxy protects it)
    - sources: UI store wins, .env only seeds it once per connection id
    - oauth: paste-back AND real HTTPS callback (a public HTTPS hostname is available to the owner)
```

## Summary

Make the database the authority for connections, without rewriting any collector: keep `pipeline/health.py:_env()` as the single choke point and let it overlay values materialized from an encrypted connection store, using the same env-style key names the collectors already read. Secrets are write-only through the API. Because there is no login, the real controls are (1) write-only responses and redaction, (2) an Origin allow-list plus a required custom header on every mutating route, and (3) encryption at rest under a key that lives in the environment, not next to the data.

Browser sign-in has two paths. **Paste-back** works with today's client registrations and needs no public host. A **real HTTPS callback** works when the owner has a public HTTPS hostname; for Google it needs a separate Web-type client.

## Findings

### 1. The `_env()` overlay is the smallest seam (codebase)

Every credential read funnels through `pipeline/health.py:_env()` (`health.py:60-72`). The overlay holds, with six corrections:

1. The store must *replace* the connection key families, not add to them: in the container `os.environ` already holds the whole `.env` (compose `env_file`), and `_env()` lets `os.environ` win, so a deleted connection would resurrect. Split `_raw_env()` (today's body, used only for seeding) from `_env()` (raw minus family keys, plus materialized store keys).
2. Labels: the regexes accept `[A-Z0-9]+` and lowercase them, so store labels are `[a-z0-9]+` and materialize upper-cased. The M365 alias becomes a file name (`tokens/<alias>_cache.bin`) with no sanitizing today: validate `[A-Za-z0-9-]+`.
3. `M365_ORG<n>` numbering is only an internal join key: number 1..N by stable creation order.
4. Zoom is a singleton (`source == "zoom"`, unprefixed keys).
5. `_env()` is called dozens of times per run from scheduler, background-task and request threads. A UI edit mid-run could make `known_sources()` and `dispatch()` disagree: freeze a `contextvars` snapshot for the duration of `execute_run`, and offer an `env_override()` for testing candidate credentials.
6. Cache the decrypted overlay in memory, invalidated by a version bump on each store write (single uvicorn process). `config_store.load_config` holds a lock while it calls `known_sources()`: the store must never call `load_config`/`save_config`.

The MSAL cache files are not covered by the overlay: they stay files. Their read-modify-write has no lock and writes are not atomic (`health.py:193-204`, `graph.py:55-65`); add a per-alias lock and an atomic 0600 write.

### 2. Secret leak surface today (codebase)

No credential is confirmed leaking, but several unbounded strings reach the DB and UI: `HealthResult.detail` and `CollectionResult.detail` (which include `str(exc)` and `resp.text[:200]` of upstream error bodies) persist into `SourceHealth.detail` / `Run.error` and are returned by `GET /api/status` and `GET /api/runs*`. New endpoints multiply that surface. Take secrets in JSON bodies only (uvicorn access logs record query strings), and apply one `redact()` at persist points and API boundaries.

### 3. Collector state and control (codebase)

Exists: `dispatch()` is a side-effect-free single-source run; `pipeline/collect.py:report()` prints one; `get_cursor` gives watermark and failure streak. Missing: a pure `dry_run()` returning data, paged item listing, a per-source "last collection result" (only run-level strings exist), and a reset that respects the window math (`collection_window` floors at 7 days and treats a watermark newer than about 24 hours as "no cursor", so "reset" is meaningful only for 1 to 7 days back). A persisting single-source run must go through the same running-run guard, which is a non-atomic check-then-insert today: add a lock. `CollectedItem.run_id` is a non-null FK, so a persisting single-source run needs a `Run` row.

### 4. Google OAuth redirect rules (domain)

- Web client: "Redirect URIs must use the HTTPS scheme, not plain HTTP. Localhost URIs (including localhost IP address URIs) are exempt from this rule." and "Hosts cannot be raw IP addresses. Localhost IP addresses are exempted from this rule." (https://developers.google.com/identity/protocols/oauth2/web-server). So a `.lan` name or a private IP is not valid; a real registered domain over HTTPS is.
- Desktop client: no redirect registration; loopback `http://127.0.0.1:port` only, "Custom URI schemes are no longer supported" (https://developers.google.com/identity/protocols/oauth2/native-app, updated 2026-09-14). Loopback stays supported for desktop apps (https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration).
- Consequence: paste-back keeps working with the existing Desktop client; the automatic callback needs a Web client, i.e. a different client id/secret. One Web client could hold both a loopback and an HTTPS URI, but any-port loopback matching for Web clients is `UNVERIFIED`. So the store keeps a client id/secret **per Gmail connection**.
- Restricted scope, personal use: the consent screen must be "In production" or refresh tokens expire after 7 days (https://support.google.com/cloud/answer/15549945); refresh tokens are capped at 100 per account per client.

### 5. Microsoft Entra redirect rules and MSAL (domain)

- `http://localhost` matches any port; http is only allowed for localhost; `[::1]` is unsupported (https://learn.microsoft.com/en-us/entra/identity-platform/reply-url).
- Client type follows the redirect URI type: "A redirect URI of type Web classifies the application as a confidential client." and "…of type Mobile and desktop applications classifies the application as a public client." A custom `https://<host>/…` URI under **Mobile and desktop applications** keeps it public (no secret, PKCE); do not use Web or SPA. Whether the portal accepts an arbitrary https custom URI there is implied, not stated (`UNVERIFIED`).
- One registration serves both flows. MSAL Python's two-step pair `initiate_auth_code_flow` + `acquire_token_by_auth_code_flow` exists on `PublicClientApplication` (inherited from `ClientApplication`; verified in the installed msal source), carries PKCE S256 and state, and its flow dict (state, redirect_uri, code_verifier, scope, auth_uri) is JSON-serializable, so keep it server-side only. MSAL string-compares state; single use and TTL are the app's job.
- Authorization codes are short-lived: "Typically, they expire after about 1 minute", so the paste-back UI must submit immediately. Paste-back needs the default `response_mode=query`; `form_post` puts the code in a POST body, not the address bar.

### 6. Security pattern for an admin UI doing OAuth (domain)

RFC 9700 basis: unguessable one-time `state` "securely bound to the user agent", codes single use, exact redirect matching (except loopback port), and "Clients and authorization servers MUST NOT expose URLs that forward the user's browser to arbitrary URIs obtained from a query parameter." Design that follows:

- A pending-flow record (state, provider, connection id, flow dict, redirect_uri, created/expires, nonce) held server-side with a 10 to 15 minute TTL and consumed atomically on first use.
- Without user accounts, bind the flow to the browser that started it with an HttpOnly, SameSite=Lax nonce cookie set at `start` (Lax cookies are sent on the provider's top-level GET redirect). A forged callback fails on unknown or unbound state.
- Provider-specific callback paths (mix-up defense); callback URL built from configured `PUBLIC_BASE_URL`, never from the request `Host`; post-callback redirect goes to a fixed UI route; never log `code` or the full URL.
- Paste-back: parse only, never fetch or redirect to the pasted URL; require scheme http, host exactly `127.0.0.1` or `localhost`, no userinfo, length cap (about 4 KB); take only `code`, `state`, `error`, `error_description`; send the stored redirect_uri at exchange, not the pasted one.

### 7. Encryption at rest and key management (library)

- `cryptography` Fernet / MultiFernet: current `cryptography 50.0.1` (Apache-2.0 OR BSD-3-Clause), abi3 manylinux wheels for x86_64 and aarch64, installs on Python 3.12 (https://pypi.org/pypi/cryptography/json, checked 2026-09-21). Fernet is AES-128-CBC with PKCS7 plus HMAC-SHA256. PyNaCl was rejected: same protection, a second native dependency, manual nonce handling, no rotation helper.
- Key management, honest table: an env-var key protects against a copy or backup of `data/` leaking alone; a key file inside `./data` protects almost nothing against a backup of `./data` (it carries key and data); a passphrase defeats unattended restarts. **Decision:** key from `SIGNALSLATE_SECRET_KEY` (comma-separated list, first is primary, for MultiFernet rotation), fail closed when unset: the app then keeps running on `.env` exactly as today and the UI shows a banner. There is no generated key file.
- What it does **not** give: the app must decrypt to use secrets, and there is no login, so anyone who can drive the API can make the app *use* a secret. Encryption at rest only mitigates data-at-rest leakage.
- Storage: nullable TEXT ciphertext column encrypted and decrypted in one service module (greppable call sites), not a `TypeDecorator`: implicit decrypt on every ORM load would put plaintext into list endpoints and a wrong key would raise inside plain SELECTs. `_add_missing_columns()` handles a nullable TEXT column natively; it silently skips NOT NULL columns without a Python default.

### 8. Write-only secrets in FastAPI 0.115 / Pydantic v2 (library, verified by experiment on the pinned versions)

- `SecretStr` masks in `repr`, logs and JSON; plaintext only via `.get_secret_value()`. Use explicit response models: an undeclared key is dropped ("leak: {'label': 'a', 'has_token': True}"), a hard backstop.
- **The default 422 handler echoes submitted secrets** (`"input":"SHORT-SECRET"`, a whole-body echo on a `model_validator` failure, raw body on a wrong content type). `hide_input_in_errors=True` does not help under FastAPI. Install a `RequestValidationError` handler that drops `input`, `ctx` and `url`, and never copy the FastAPI docs example, which returns `exc.body`. Add a regression test that posts a too-short secret and asserts the string is absent from the response.
- `TestClient` needs `httpx` (absent from requirements.txt): pin it (resolved `httpx 0.28.1` against the pinned FastAPI/Starlette).

### 9. CORS and CSRF for an unauthenticated LAN API (library, verified)

- With today's `allow_origins=["*"]` a custom-header defense is void. A strict CORS config does not stop the write either: "strict simple POST from evil origin: server ran handler -> 200"; the browser only withholds the response. Server-side rejection is required, and body-less POSTs (test connection, run now) are the exposed ones.
- Plan: explicit origin allow-list; a global guard on non-safe methods that requires a custom header (preflight-triggering) and rejects a present `Origin` not on the list; require `Content-Type: application/json` on body routes; optionally `Sec-Fetch-Site`. A Host-header allow-list against DNS rebinding is `UNVERIFIED`.

### 10. Frontend verify story (library, ran in a scratch copy)

Nothing runs before `npm ci` (no `node_modules`, no `tsconfig.json`, no tests). `npm run build` works (about 7 s) but does not type-check. `nuxi typecheck` needs `vue-tsc`, `typescript` and a root `tsconfig.json`. **Trap:** `typescript` 7.x crashes vue-tsc 3.3.11 (`ERR_PACKAGE_PATH_NOT_EXPORTED`); pin `typescript ~6.0.3`. Two pre-existing typecheck errors surface immediately: `extras.icons` is not a valid key for nuxt-quasar-ui 3.1.1 (`fontIcons`), and `process` needs `@types/node`. Smallest devDependency set that ran a component test (`mountSuspended` on a password `q-input`): `typescript ~6.0.3`, `vue-tsc ^3.3.11`, `@types/node ^24`, `vitest ^5.0.1`, `@vue/test-utils ^2.5.1`, `@nuxt/test-utils ^4.3.2`, `happy-dom ^20.14.5`. Verify chain: `npm ci && npm run typecheck && npm test && npm run build` (about 45 s cold).

## Decisions (PROPOSAL, drives the plan)

1. Connection store (SQLite `connection` + `tombstone` tables, encrypted secrets envelope) materializing env-style keys through the `_env()` overlay; store wins; `.env` seeds once per id; deletes are tombstoned; no key means no overlay (env-only, today's behavior).
2. Settings: `SIGNALSLATE_SECRET_KEY`, `WEB_ORIGINS`, `PUBLIC_BASE_URL` (all optional).
3. Ids equal source ids (`m365_<alias>`, `zoom`, `slack_<label>`, `gmail_<label>`); each Gmail connection carries its own client id/secret.
4. Both sign-in modes; the callback mode is offered only when `PUBLIC_BASE_URL` is https.
5. No app login; the controls in findings 8 and 9 plus redaction.

## Open questions

- Does the Entra portal accept an arbitrary https custom redirect under "Mobile and desktop applications"? (check when registering)
- Any-port loopback matching for a Google Web client (not needed if paste-back keeps using the Desktop client).
- Whether `extras.icons` was silently ignored at runtime (the rename to `fontIcons` should be checked visually).
- Whether the owner's public hostname already terminates TLS in front of the API.

## References

- https://developers.google.com/identity/protocols/oauth2/web-server
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration
- https://support.google.com/cloud/answer/15549945
- https://learn.microsoft.com/en-us/entra/identity-platform/reply-url
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/confidential-client-application-authentication-error-aadsts7000218
- https://msal-python.readthedocs.io/en/latest/
- https://datatracker.ietf.org/doc/html/rfc9700
- https://datatracker.ietf.org/doc/html/rfc8252
- https://cryptography.io/en/latest/fernet/
- https://pypi.org/pypi/cryptography/json
- https://fastapi.tiangolo.com/tutorial/handling-errors/
- https://fastapi.tiangolo.com/tutorial/cors/
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS
- https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Request_Forgery_Prevention_Cheat_Sheet.html
- https://quasar.dev/vue-components/dialog
- https://quasar.dev/vue-components/input
- https://nuxt.com/docs/3.x/getting-started/testing
- https://nuxt.com/docs/3.x/guide/concepts/typescript
