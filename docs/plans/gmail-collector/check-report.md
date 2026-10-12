---
result: FAIL
ts: 2026-10-12T05:34:21Z
ref: deca0d4881f293d68be323e408a271a63ae3c9ab
scope: plan
plan: gmail-collector
---

# Check — gmail-collector (re-check on main after the 2026-10-12 repo review)

**Result:** FAIL · **Base:** 60ff68a (parent of the plan's first `Task:` commit; 55 commits) · **Files:** 29 · **LOC:** +10093 / -169

## Summary

The plan's behaviour is intact and verified: every stated Outcome in spec.md is implemented and wired (three survey lenses), all 25 critic-authored held-out checks pass, and the full suites are green from a clean checkout. FAIL comes from two stale `verify[]` commands: T-007 and T-024 grep `auth/gmail_bootstrap.py` for `def build_auth_url` and `isinstance(.*dict`, which management-ui T-014 (a6388b4, 2026-09-21, after this plan merged) moved into `pipeline/oauth_google.py`. Only `scripts/tasks.sh` may write `tasks.json` and it cannot edit `verify[]`, so both tasks are now `blocked: test-assertion-suspect` for a human to re-point the checks (one path each); the loop escalates on the next tick. A human still owns: that ruling, and the two advisory-band survey warnings under Later.

## Gates

| Gate | Before fix | After fix | Status |
|---|---|---|---|
| typecheck (web, `nuxt typecheck`; ratchet `type_errors`) | 0 errors | — | PASS |
| typecheck (python, pyright non-strict) | 64 diagnostics (baseline 68, none new) | — | PASS (advisory) |
| typecheck (python, toolchain mypy lane) | exit 2, 279 diagnostics: 249 "Cannot find implementation" (no `__init__.py` in auth/ and tests/, untyped third-party), 29 missing stubs, 1 duplicate source | — | SKIPPED: unconfigured (no mypy config in the repo; see Later) |
| lint (`ruff --select F,E9`, repo convention: no config) | 1 error (pre-existing F541 in `tests/test_domain_rdap.py:108`, outside this plan) | — | PASS (advisory) |
| lint (toolchain lane) | runner invoked `ruff check a.py` (E902) | — | ERROR: lane runner misuse, no verdict |
| tests (python, full) | 3472/3474 passed · 2 xfailed · escaped 0 | — | PASS |
| tests (web, vitest) | 528/528 passed | — | PASS |
| build | no build step for the python stack; web build out of this plan's scope | — | SKIPPED |

TIA selected/full split not applicable (pytest; one full run).

## Tasks (plan scope)

| Task | passes | last_verify.ok | failed | tail |
|---|---|---|---|---|
| T-001 | true | true | — | — |
| T-002 | true | true | — | — |
| T-003 | true | true | — | — |
| T-004 | true | true | — | — |
| T-005 | true | true | — | — |
| T-006 | true | true | — | — |
| T-007 | false | - | `grep -qE 'def build_auth_url' auth/gmail_bootstrap.py` | — |
| T-008 | true | true | — | — |
| T-009 | true | true | — | — |
| T-010 | true | true | — | — |
| T-011 | true | true | — | — |
| T-012 | true | true | — | — |
| T-013 | true | true | — | — |
| T-014 | true | true | — | — |
| T-015 | true | true | — | — |
| T-016 | true | true | — | — |
| T-017 | true | true | — | — |
| T-018 | true | true | — | — |
| T-019 | true | true | — | — |
| T-020 | true | true | — | — |
| T-021 | true | true | — | — |
| T-022 | true | true | — | — |
| T-023 | true | true | — | — |
| T-024 | false | - | `grep -qE 'isinstance\(.*dict' auth/gmail_bootstrap.py` | — |
| T-025 | true | true | — | — |

T-007 and T-024: `status: blocked`, `blocked_reason: test-assertion-suspect` (set by this check through `tasks.sh set`). Ruling needed: re-point `verify[0]` of each at `pipeline/oauth_google.py` (`def build_auth_url` at :57; `isinstance(body, dict)` at :141), then `tasks.sh verify gmail-collector T-007` and `T-024`.

## Held-out checks (critic-authored, plan scope)

| Task | ok | Command | Tail |
|---|---|---|---|
| T-001 | true | `timeout 60 venv/bin/python scratchpad/ho/T-001.py (pytest --collect-only tests + core deps` | OK collect-only rc=0, 3474 tests collected in 2.65s; core deps declared in requi |
| T-002 | true | `timeout 60 venv/bin/python scratchpad/ho/T-002.py (env_override + mocked token/profile: kn` | OK known_sources lists gmail_<label>; check_gmail: unsigned/scope-missing/invali |
| T-003 | true | `timeout 60 venv/bin/python scratchpad/ho/T-003.py (runner._active with undeclared gmail so` | OK runner._active filters by known_sources: ['gmail_heldout'] |
| T-004 | true | `timeout 60 venv/bin/python scratchpad/ho/T-004.py (list_message_ids 2 pages, UTC epoch q, ` | OK 2 pages drained, UTC epoch q, 403 rate-limit retried, 403 permission not retr |
| T-005 | true | `timeout 60 venv/bin/python scratchpad/ho/T-005.py (flatten_message nested multipart + html` | OK plain preferred, html fallback, attachment meta: [{'filename': 'inv.pdf', 'mi |
| T-006 | true | `timeout 60 venv/bin/python scratchpad/ho/T-006.py (dispatch('gmail_heldout') end-to-end wi` | OK dispatch routes gmail_<label>; dedupe + internalDate window; missing token -> |
| T-007 | true | `timeout 60 venv/bin/python scratchpad/ho/T-007.py (make_pkce/build_auth_url via bootstrap ` | OK PKCE S256 + offline/consent URL; loopback wait_for_code returns code, rejects |
| T-008 | true | `timeout 60 venv/bin/python scratchpad/ho/T-008.py (.env.example GMAIL_ keys through health` | OK .env.example GMAIL keys parse through health's regex -> ['GMAIL_CLIENT_ID', ' |
| T-009 | true | `timeout 60 venv/bin/python scratchpad/ho/T-009.py (normalize_gmail roles/outbound/bulk/quo` | OK normalize_gmail: roles, outbound, bulk, quote strip, snippet unescape fallbac |
| T-010 | true | `timeout 60 venv/bin/python scratchpad/ho/T-010.py (extra=forbid, validate_batch unknown/du` | OK extra=forbid, validate_batch order/unknown/dup/missing, bad due dropped, bulk |
| T-011 | true | `timeout 60 venv/bin/python scratchpad/ho/T-011.py (anthropic pin == installed; blank key -` | OK anthropic pinned 1.7.0 == installed; blank key -> TriageConfigError; key pass |
| T-012 | true | `timeout 60 venv/bin/python scratchpad/ho/T-012.py (fake client: omitted alias retried once` | OK retry-once then stub, failed batch stubbed, failures=['batch 2: call failed,  |
| T-013 | true | `timeout 60 venv/bin/python scratchpad/ho/T-013.py (novel hostile body/subject; echoed URL/` | OK hostile body confined to one JSON string; echoed output sanitized -> 'Click h |
| T-014 | true | `timeout 60 venv/bin/python scratchpad/ho/T-014.py (tmp sqlite: items_for_source_since wind` | OK non-gmail source exit 2; items_for_source_since window+order; --dry prints 2  |
| T-015 | true | `timeout 60 venv/bin/python scratchpad/ho/T-015.py (_participant_line + build_request parti` | OK participants role-prefixed: ['from: Ann <a@example.com>', 'to: me@example.com |
| T-016 | true | `timeout 60 venv/bin/python scratchpad/ho/T-016.py (sanitize_text vbscript/file/data/ftp/ma` | OK risky schemes and //host stripped; label lookalikes and bare domains kept |
| T-017 | true | `timeout 60 venv/bin/python scratchpad/ho/T-017.py (collect_gmail with one raising message ` | OK one failing message -> partial with 2 items; unknown charset decoded; detail= |
| T-018 | true | `timeout 60 venv/bin/python scratchpad/ho/T-018.py (1.5M-char hostile HTML timing + bodyTru` | OK hostile 1500010 chars parsed in 0.33s, cut flagged; oversized html -> bodyTru |
| T-019 | true | `timeout 60 venv/bin/python scratchpad/ho/T-019.py (idle raw socket held open during real c` | OK idle connection did not block callback; idle socket closed after 4.9s |
| T-020 | true | `timeout 60 venv/bin/python scratchpad/ho/T-020.py (_text_charset denylist incl. base64/rot` | OK non-text codecs rejected; flattened payload UTF-8-encodable with surrogate in |
| T-021 | true | `timeout 60 venv/bin/python scratchpad/ho/T-021.py (RuntimeError/UnicodeEncodeError/JSONDec` | OK every Exception class degrades to stubs (class name only); KeyboardInterrupt  |
| T-022 | true | `timeout 60 venv/bin/python scratchpad/ho/T-022.py (ascii stdout + lone surrogates through ` | OK --dry on ascii stdout with lone surrogates exits 0 with backslash escapes; DB |
| T-023 | true | `timeout 60 venv/bin/python scratchpad/ho/T-023.py (<span style=display:none/>, <div hidden` | OK self-closing hidden tags hide following text; head/title skipped; void self-c |
| T-024 | true | `timeout 60 venv/bin/python scratchpad/ho/T-024.py (gb.exchange_code: list/str/non-JSON/ESC` | OK non-dict/non-JSON/hostile token bodies -> TokenExchangeError, bounded, code n |
| T-025 | true | `timeout 60 venv/bin/python scratchpad/ho/T-025.py (ESC/BEL/URL/md-js/markup/500-char sende` | OK hostile sender names sanitized and bounded through stub_record and triage_ite |

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| Does Gmail return Subject/From headers large enough to make the quadratic `sanitize_text` cost material? | fixture or documented header limit | measured on the real function: 1 KB 1.1 ms, 8 KB 68 ms, 80 KB 7.1 s; RFC 5322 lines are capped at 998 chars, so realistic (even folded) subjects stay in the ms range. Recorded as a Later item, not a defect. |
| Does a 7-day backfill on a busy mailbox finish within the 1 h access-token lifetime? | live dry run (`python -m pipeline.collect gmail_<label> --raw --hours 168`) | arithmetic from the constants: 0.3 s pace + ~0.3 s latency ≈ 6000 gets per hour; a mailbox above that produces a partial result and a held-back cursor (no data loss), so this is a Later item. |

## Findings

### Critical (blocks)

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | docs/plans/gmail-collector/tasks.json T-007 verify[0] | 🔴 `grep -qE 'def build_auth_url' auth/gmail_bootstrap.py` fails: the function moved to `pipeline/oauth_google.py:57` in a6388b4 (management-ui T-014); the bootstrap imports it (`auth/gmail_bootstrap.py:52`) and `tests/test_gmail_bootstrap.py` passes. Re-point the check. | verify T-007 / critic | 1.0 |
| 2 | docs/plans/gmail-collector/tasks.json T-024 verify[0] | 🔴 `grep -qE 'isinstance\(.*dict' auth/gmail_bootstrap.py` fails: the guard moved to `pipeline/oauth_google.py:141` in a6388b4; behaviour reproduces through `exchange_code` (held-out check T-024 ok). Re-point the check. | verify T-024 / critic | 1.0 |

### Major

LGTM

### Minor

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 3 | pipeline/triage.py:133,135,164-168 | 🔵 `sanitize_text` runs the backtracking `_HTML`/`_MD_LINK` regexes before `truncate()`; `stub_record` feeds it the uncapped subject. Measured 80 KB → 7.1 s, 8 KB → 68 ms. Cap the input to a multiple of `max_len` before the regexes. | survey:security (FP-verified) | 0.75 |
| 4 | pipeline/collectors/gmail.py:487 | 🔵 one access token per `collect_gmail` call, never refreshed; 401 is non-retryable, so a backfill longer than the token lifetime (~6000 paced gets) ends partial and re-fetches next run. Refresh on 401 once, or every N gets. | survey:backend | 0.50 |

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 5 | pipeline/health.py:237,252,274,529 | det-06 hits are `(x or "").strip()` normalizations, not fallback values; dropped | det-06 (FP-verified) | — |
| 6 | tests/test_collectors.py | check:test-tamper: two `assert` lines removed vs base were replaced by stronger asserts in zoom-collector T-006; assertion count 190 → 5522 | check:test-tamper (FP-verified) | — |
| 7 | pipeline/runner.py:72 | det-10: `return []` in `_stored_secrets` when no vault is active; intended | det-10 | — |
| 8 | auth/gmail_bootstrap.py:204, pipeline/collectors/gmail.py:441 | two narrowly scoped `# type: ignore[...]` with local justification (python counterpart of det-04) | critic (advisory) | — |

## Auto-fix (`--fix`)

| Category | Found | Fixed | Remaining | Skipped |
|---|---|---|---|---|
| Imports / exports | 0 | 0 | 0 | 0 |
| Type errors | 0 | 0 | 0 | 0 |
| Lint | 0 in scope | 0 | 0 | 1 pre-existing F541 outside scope |
| Framework (F5/V3/P2) | n/a (no Vue/Firestore pack for this stack) | — | — | — |
| Naming / return types | 0 | 0 | 0 | 0 |
| Unused | 0 | 0 | 0 | 0 |

Commits: none. Manual fixes: the two `verify[]` paths (Before merge); `--fix` never edits `tasks.json`.

## Ratchet

| Metric | Baseline | Current | Threshold | Δ | Action |
|---|---|---|---|---|---|
| type_errors | 0 | 0 | 0 (absolute) | 0 | — |
| test_count | 404 | 445 | min 441 → 445 | ↑ tightened | history appended |
| as_any_count | 0 | 0 | 0 | 0 | — |
| mocks_in_src | 0 | 0 | 0 | 0 | — |
| todo_count | 0 | 0 | 0 | 0 | — |
| stale_worktree_branch_count | 0 | 0 | 0 | 0 | — |
| lint_violations / completeness_score | null | null | null | — | not measured (no linter config) |

## Critic

`--mode reject` verdict: REJECT (derived: 2 confirmed reject findings = verify T-007, verify T-024) · mode: in-Claude · effort: high · findings: 5 · held-out: 25/25 ok · redacted: 0

## Security posture

registry-validate: ok · startup-validate --strict: ok (run before the verify side effect; T-007/T-024 are now `blocked`, which the contract allows) · pre-trust execution: none

## Automation coverage

Deterministic gates: 7/9 (web typecheck, pyright, ruff F, pytest, vitest, ratchet, registry rows pass; mypy lane skipped unconfigured, toolchain lint lane error) · critic: REJECT on stale checks · e2e: skipped_unavailable (no rendered surface in this plan)
Not auto-verified (human owns): architectural fit, UX correctness, business-logic intent, regressions in untested paths.
**Recommendation:** needs-human-review

## Before merge

1. Re-point `docs/plans/gmail-collector/tasks.json` T-007 verify[0] to `grep -qE 'def build_auth_url' pipeline/oauth_google.py` and T-024 verify[0] to `grep -qE 'isinstance\(.*dict' pipeline/oauth_google.py`, then run `tasks.sh verify gmail-collector T-007` and `T-024` (both then pass; held-out checks already prove the behaviour).

## Later

1. `pipeline/triage.py:164`: cap `text` to about `max_len * 20` before the regex passes in `sanitize_text` (Minor 3).
2. `pipeline/collectors/gmail.py:487`: refresh the access token once on a 401 (Minor 4).
3. Adopt a python type gate the toolchain can run (pyright config, or mypy with `explicit_package_bases` and stubs), so the typecheck lane stops reading as unconfigured; folds into the research doc's T-09 lint-config task.
4. Prefer outcome-based `verify[]` checks over file-path greps when planning: both failures here were artifact checks forward-coupled to a path a later refactor moved (docs/research/2026-10-12_repo-review.md already notes the pattern).
