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
## 2026-09-21T02:54:33Z build T-008 start (attempt 1)
## 2026-09-21T02:55:38Z verify T-008 ok=true
## 2026-09-21T02:56:02Z build T-009 start (attempt 1)
Ruling: defer — dev added '# pyright: ignore[reportMissingImports]' to pydantic/pytest imports in pipeline/normalize.py and two test files because the post-edit pyright hook does not resolve venv/. That is a tooling gap, not a code defect; the clean fix is a pyrightconfig.json (venvPath/venv) and removing the ignores. Not done here (out of scope of every task). NormalizedItem.attachments are Attachment models (use .model_dump() for dicts); PayloadError added. (T-009, round 1)
## 2026-09-21T02:59:19Z verify T-009 ok=true
## 2026-09-21T02:59:19Z build T-010 start (attempt 1)
Ruling: defer — sanitize_record's due check is stricter than fromisoformat on purpose (YYYY-MM-DD only; 3.12 fromisoformat also accepts 20260920 and ISO week dates). stub_record falls back to NO_SUBJECT for an empty or link-only title so a stub digest line is never blank. (T-010, round 1)
## 2026-09-21T03:02:47Z verify T-010 ok=true
## 2026-09-21T03:02:47Z build T-011 start (attempt 1)
Ruling: defer — anthropic==1.7.0 pinned (venv/bin/pip check clean; no existing pin changed). Its transitive deps are unpinned by design (flat direct-pin style): httpx2/httpcore2 (2.13.0, repo pydantic/httpx2), truststore, jiter, sniffio, docstring-parser. Provenance was checked against anthropic's own declared Requires-Dist metadata (it requires httpx2<3,>=2.0.0), not against PyPI publisher records; re-check when the Docker image is first built. The post-edit pyright hook's baseline (76 -> 79) is stale; project-wide count is 79 before and after. (T-011, round 1)
## 2026-09-21T03:04:57Z verify T-011 ok=true
## 2026-09-21T03:05:21Z build T-012 start (attempt 1)
Ruling: split — build_request sends participants as bare 'Name <addr>' strings (my spec), so the model cannot tell sender from recipients, which weakens action_needed. Fix is a new task T-015 (role prefix per participant) rather than widening T-012; it also drops tests' direct import of httpx2, a transitive dependency of the pinned SDK. Runs after T-013/T-014, which do not depend on the participant format. (T-012, round 1)
## 2026-09-21T03:08:46Z verify T-012 ok=true
## 2026-09-21T03:08:46Z build T-013 start (attempt 1)
Ruling: split — sanitize_text only strips http(s):// and www. URLs, so ftp:, mailto:, javascript:, data:, file: and protocol-relative //host forms in a model reply survive into the digest; T-013's tests pin only what T-010 specified. New task T-016 hardens the sanitizer (explicit risky schemes and //host; bare domains are deliberately left alone: too many false positives on company names). The white-on-white/font-size:0 leak stays a recorded known limit with a test that fails on purpose when stripping is extended. (T-013, round 1)
## 2026-09-21T03:16:30Z verify T-013 ok=true
## 2026-09-21T03:16:30Z build T-014 start (attempt 1)
Ruling: defer — triage_cli live mode triages every item in the window (--limit only caps what is printed), as specified; a large --hours window spends API credit on all of it. The WAL pragma on open may leave -wal/-shm sidecar files next to the DB (no rows written). Rows that are all unreadable payloads report 'no items' and exit 0. The post-edit typecheck hook baseline (76) is stale against a repo-wide pyright count of 85 that is identical with and without these changes (unresolved venv imports); a pyrightconfig.json with the venv path would fix the cause and is left to the user. (T-014, round 1)
## 2026-09-21T03:19:50Z verify T-014 ok=true
## 2026-09-21T03:19:50Z build T-015 start (attempt 1)
## 2026-09-21T03:20:49Z verify T-015 ok=true
## 2026-09-21T03:20:49Z build T-016 start (attempt 1)
Ruling: defer — sanitize_text now removes any scheme:// URL (generic, a superset of the suggested list), opaque schemes mailto/javascript/vbscript/data/tel/sms/blob when immediately followed by a non-space, and //host; bare domains and label lookalikes (Data: Q3, Tel: 555, metadata:x) are kept. _MD_LINK allows one level of nested parens in the target (Tier 2 deviation, disjoint alternatives, no catastrophic backtracking; deeper nesting leaves a trailing ')'). (T-016, round 1)
## 2026-09-21T03:22:52Z verify T-016 ok=true
## 2026-09-21T04:06:23Z check FAIL check-report.md
## 2026-09-21T04:07:52Z build T-017 start (attempt 1)
## 2026-09-21T04:10:25Z verify T-017 ok=true
## 2026-09-21T04:35:16Z build T-018 start (attempt 1)
## 2026-09-21T04:38:02Z verify T-018 ok=true
## 2026-09-21T04:38:02Z build T-019 start (attempt 1)
## 2026-09-21T04:41:07Z verify T-019 ok=true
## 2026-09-21T05:09:39Z check FAIL check-report.md
## 2026-09-21T05:11:41Z build T-020 start (attempt 1)
## 2026-09-21T05:14:06Z verify T-020 ok=true
