## 2026-09-21T22:28:12Z plan created
Ruling: class=architectural tasks=30 — new pipeline/domains subsystem (own tables, 2nd scheduler job, 3 registrar kinds, 2 routers, console page); purchase ships disabled behind server-side guards.
## 2026-09-21T22:31:41Z build T-001 start (attempt 1)
Ruling: accept — requests 2.32.3→2.34.2 forced by whoisit>=4 peer constraint (requests>=2.32.5); full suite re-run (T-001, round 1)
## 2026-09-21T22:32:49Z verify T-001 ok=true
## 2026-09-21T22:33:01Z build T-002 start (attempt 1)
## 2026-09-21T22:36:12Z verify T-002 ok=true
## 2026-09-21T22:36:12Z build T-003 start (attempt 1)
## 2026-09-21T22:40:35Z verify T-003 ok=true
## 2026-09-21T22:40:35Z build T-004 start (attempt 1)
## 2026-09-21T22:45:15Z verify T-004 ok=true
## 2026-09-21T22:45:15Z build T-005 start (attempt 1)
Ruling: fix — non-premium Namecheap quote had price 0 (would bypass price cap); round 1 fetches users.getPricing, raises when unresolvable (T-005, round 1)
## 2026-09-21T22:55:31Z verify T-005 ok=true
## 2026-09-21T22:55:31Z build T-006 start (attempt 1)
Ruling: defer — T-007 waits for peer management-ui T-037 (pipeline/oauth_flows.py); running independent tasks first (T-006)
## 2026-09-21T23:00:14Z verify T-006 ok=true
