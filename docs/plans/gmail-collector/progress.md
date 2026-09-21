## 2026-09-21T02:26:33Z plan created
Ruling: class=architectural tasks=14 — new collector, auth bootstrap and health check plus six wiring points, extended with the first Phase 3 slice (user-chosen scope); research supplied via --from-research, no agents spawned.
## 2026-09-21T02:32:36Z build T-001 start (attempt 1)
## 2026-09-21T02:34:39Z verify T-001 ok=true
Ruling: defer — dev environment is a venv built on mise Python 3.12.14, not the host default 3.14.7, to match the python:3.12-slim image; baseline suite 120 passed. README should note this under Local dev when T-008 edits it (T-001 had no README scope). (T-001, round 1)
## 2026-09-21T02:38:17Z build T-002 start (attempt 1)
## 2026-09-21T02:40:54Z verify T-002 ok=true
Ruling: spec-defect — the plan's stub-grep matched the pre-existing MSTODO_ env prefix and could never pass on pipeline/health.py; tasks.sh cannot edit verify[] after add, so tasks.json was regenerated through tasks.sh with a word-boundary pattern (\bTODO\b, \bFIXME\b) and T-001/T-002 re-verified (both PASS). No task content changed otherwise. (T-002, round 1)
Ruling: defer — dev added a gmail branch to check_all_configured() that the task notes did not list explicitly; needed so active-source toggles reach check_gmail. FakeResponse in tests/test_health.py gained status_code/raise_for_status (existing tests unaffected). (T-002, round 1)
## 2026-09-21T02:40:54Z build T-003 start (attempt 1)
## 2026-09-21T02:42:05Z verify T-003 ok=true
## 2026-09-21T02:42:10Z build T-004 start (attempt 1)
## 2026-09-21T02:44:11Z verify T-004 ok=true
## 2026-09-21T02:44:18Z build T-005 start (attempt 1)
Ruling: defer — hidden-content stripping covers display:none, visibility:hidden, hidden attr and aria-hidden only; white-on-white text, font-size:0, opacity:0 and off-screen positioning still reach bodyText. Injection defense must not rely on it alone (T-012 JSON encoding + T-013 tests). (T-005, round 1)
Ruling: defer — flatten_message passes snippet through HTML-entity-escaped; T-009's adapter must html.unescape it. fetch_messages takes id strings, list_message_ids returns {id, threadId} dicts; T-006 maps them. BODY_CAP=20000 remains a guess for the dry run. Body parts carrying only an attachmentId yield empty bodyText (attachments.get is never called). (T-005, round 1)
## 2026-09-21T02:49:16Z verify T-005 ok=true
## 2026-09-21T02:49:25Z build T-006 start (attempt 1)
## 2026-09-21T02:51:47Z verify T-006 ok=true
## 2026-09-21T02:51:47Z build T-007 start (attempt 1)
Ruling: defer — a state-mismatched loopback callback aborts the bootstrap run instead of continuing to wait (spec: reject mismatches); a stray local request can force a retry. Acceptable for a one-shot interactive CLI. main(argv) is only covered for bad-label and --help; the browser flow and healthy short-circuit are manual. (T-007, round 1)
## 2026-09-21T02:54:33Z verify T-007 ok=true
