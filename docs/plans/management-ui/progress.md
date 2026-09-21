## 2026-09-21T05:58:02Z plan created
Ruling: class=architectural tasks=31 — new subsystem across the store, API, OAuth flows and web; owner chose full scope (edit credentials, browser sign-in in both modes), no app login, store-wins with .env as seed; research supplied by three parallel agents and consolidated in docs/_research/2026-09-21_management-ui.md.
Ruling: defer — the earlier design summary said the encryption key comes from an env var else a generated key file; research showed a key file beside the database is copied by the same backup and protects almost nothing, so the plan is env-only and fails closed (env-only mode keeps working with a UI banner). Flagged to the owner at hand-off.
## 2026-09-21T06:01:51Z build T-001 start (attempt 1)
## 2026-09-21T06:02:52Z verify T-001 ok=true
## 2026-09-21T06:02:52Z build T-002 start (attempt 1)
## 2026-09-21T06:04:51Z verify T-002 ok=true
## 2026-09-21T06:04:51Z build T-003 start (attempt 1)
Ruling: defer — redact(): a bare 1-3 digit 'code' value is intentionally kept (HTTP status in upstream bodies); unquoted key=value pair values stop at whitespace/quote/&/,/;/}/] so a secret containing those relies on the supplied-secret match; it fails closed to the marker on an internal error. (T-003, round 1)
## 2026-09-21T06:10:21Z verify T-003 ok=true
## 2026-09-21T06:10:21Z build T-004 start (attempt 1)
## 2026-09-21T06:11:46Z verify T-004 ok=true
## 2026-09-21T06:11:46Z build T-005 start (attempt 1)
## 2026-09-21T06:13:29Z verify T-005 ok=true
## 2026-09-21T06:13:30Z build T-006 start (attempt 1)
Ruling: defer — connections.secrets_set is derived by decrypting the envelope, so with no vault or a wrong key a view reports [] (the list page stays readable) and update-with-secrets on an undecryptable row raises SecretDecryptError (recovery: delete and recreate). T-007's notes were extended at build time with a narrow public get_secret(id, name) accessor because T-016 needs the Gmail client_secret and _secrets_for must stay private. (T-006, round 1)
## 2026-09-21T06:18:57Z verify T-006 ok=true
## 2026-09-21T06:18:57Z build T-007 start (attempt 1)
Ruling: defer — overlay_provider returns a read-only MappingProxyType keyed on version plus vault identity; materialize/secret_values skip rows they cannot decrypt (warning names the id only) while get_secret raises the generic crypto errors; blank primary env values count as undeclared. T-008's notes were extended accordingly. (T-007, round 1)
## 2026-09-21T06:23:34Z verify T-007 ok=true
## 2026-09-21T06:23:34Z build T-008 start (attempt 1)
Ruling: defer — tests/conftest.py gained an autouse fixture clearing the process-global env overlay provider around every test; _env() always returns a fresh dict; provider exceptions propagate out of _env() (connections.materialize handles its own decrypt errors); gmail_token_response still reads _env() twice (consistent inside a snapshot). web_origins() does not normalize trailing slashes or case: T-018's notes now require normalization on both sides of the Origin match. Seam verified independently against the real connections provider: deleted seeded connection does not resurrect from the container env, store wins after an edit, snapshot stable mid-run. (T-008, round 1)
## 2026-09-21T06:27:06Z verify T-008 ok=true
