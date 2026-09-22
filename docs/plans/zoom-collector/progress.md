## 2026-09-21T23:30:00Z plan created
Ruling: class=architectural tasks=12 — auth-mode change + new OAuth provider + collector rewrite across pipeline/api/web (≥6 files); ingested from docs/_research/2026-09-21_zoom-collector.md, --autonomous (assumptions in spec.md).
## 2026-09-21T23:45:00Z owner decisions
Ruling: transcripts on by default (include_transcripts=true); default auth mode oauth (sign-in); cross-process fcntl lock confirmed. Notes updated on T-002, T-005, T-007, T-009; spec §Assumptions split into owner decisions vs inferred.
## 2026-09-21T23:01:05Z build T-001 start (attempt 1)
## 2026-09-21T23:02:49Z verify T-001 ok=true
## 2026-09-21T23:02:49Z build T-002 start (attempt 1)
## 2026-09-21T23:09:04Z verify T-002 ok=true
Ruling: carry-forward — T-002 concern: oauth-mode zoom materializes no env keys; T-005 zoom_token must read client_id/secret/refresh_token from the connection store (connections.get/get_secret), not _env() (T-002, round 1)
## 2026-09-21T23:09:04Z build T-003 start (attempt 1)
## 2026-09-21T23:13:55Z verify T-003 ok=true
## 2026-09-21T23:14:00Z build T-004 start (attempt 1)
## 2026-09-21T23:26:40Z verify T-004 ok=true
## 2026-09-21T23:26:40Z build T-005 start (attempt 1)
## 2026-09-21T23:35:28Z verify T-005 ok=true
Ruling: defer — Pyright reportOptionalMemberAccess on _zoom_cache.as_dict() in health.py (typing only, runtime-safe); leave for /blitz:check (T-005, round 1)
## 2026-09-21T23:35:28Z build T-006 start (attempt 1)
## 2026-09-21T23:46:50Z verify T-006 ok=true
Ruling: defer — report endpoint 1-month window not clamped; window is SUMMARY_LOOKBACK (48h) unless a long outage widens it, and a report failure only makes the run partial (T-006, round 1)
## 2026-09-21T23:46:50Z build T-007 start (attempt 1)
## 2026-09-22T00:07:13Z verify T-007 ok=true (round 1 resume after ECONNRESET; no attempt consumed)
## 2026-09-22T00:07:13Z build T-008 start (attempt 1)
## 2026-09-22T00:10:53Z verify T-008 ok=true
## 2026-09-22T00:10:53Z build T-009 start (attempt 1)
## 2026-09-22T00:30:06Z verify T-009 ok=true
Ruling: scope — T-010 widened to api/routers/status.py, web/composables/useApi.ts, tests/test_api_startup.py: /api/system oauth map needs a zoom entry and OAuthProvider needs 'zoom'; public URL still never echoed (T-010, pre-dispatch)
## 2026-09-22T00:30:06Z build T-010 start (attempt 1)
## 2026-09-22T00:37:04Z verify T-010 ok=true
Ruling: accept — T-010 Tier-1 out-of-scope edit web/tests/ConnectionDialog.test.ts (fixture key zoom added, forced by OAuthProvider widening; no assertion changed; commit f141a3a) (T-010, round 1)
## 2026-09-22T00:37:04Z build T-011 start (attempt 1)
## 2026-09-22T00:47:20Z gate disarmed while dev-T-011 resumes (in-progress test file red by design); re-verify on reply
## 2026-09-22T00:50:02Z verify T-011 ok=true (round 1 resume after turn limit)
