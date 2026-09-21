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
