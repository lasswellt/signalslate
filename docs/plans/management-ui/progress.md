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
