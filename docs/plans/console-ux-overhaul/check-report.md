---
result: FAIL
ts: 2026-09-23T19:55:44Z
ref: e27212448f08bc28d3dc63e5a61a938fcee30882
scope: plan
plan: console-ux-overhaul
---

# Check — console-ux-overhaul

**Result:** FAIL · **Base:** 5cf2244 · **Files:** 72 (excl. docs/plans) · **LOC:** +9534 / −3695

## Summary

Console UX overhaul: drawer shell, shared UI kit and helpers, nested routes, two read-only API fields. The gates, tasks, ratchet and deterministic rows are clean. The reject critic returned **REJECT**: the footer Close in JobApplyDialog skips the dirty guard, so unsaved cover-letter edits are lost (T-020 held-out check). Follow-ups were added as T-022 (the blocker) and T-023 (the minor findings). A human still owns the visual/UX review, because e2e was not run.

## Gates

| Gate | Before fix | After fix | Status |
|---|---|---|---|
| typecheck (web: nuxt typecheck) | 0 errors | — | PASS |
| typecheck (python: pyright on changed files) | 0 errors | — | PASS |
| lint | no linter configured | — | SKIPPED |
| tests (web, full) | 468/468 · escaped 0 | — | PASS |
| tests (python, full) | 2903 passed | — | PASS |
| build (nuxt build) | PASS | — | PASS |

## Tasks (plan scope)

All 21 tasks (T-001…T-021) were re-verified with `tasks.sh verify` this run: 21/21 `passes: true`. New follow-ups T-022 and T-023 (origin: check) are open.

## Held-out checks (critic-authored, plan scope)

| Task | ok | Probe |
|---|---|---|
| T-001…T-019, T-021 | true | intent probes: shared Intl formatters, StatusChip icon+label, dark auto, DialogShell dirty/maximized, grouped drawer + breadcrumbs, poll cleanup, pagination/sort/not-found, presets + leave guard, kind groups, copy + reveal, dry run in menu, items route, DomainTable/no NS column, @purchased/no env jargon, no data_hash, posting_title join, debounce, profile leave guard, no local helper copies |
| T-020 | **false** | `! grep -nE '@click="close"' web/components/JobApplyDialog.vue` → L146 footer Close bypasses the dirty guard |

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| Rendered layout at 375px / 1440px, light and dark | running app + Playwright | not run. Starting the API locally uses the real `data/digest.db` and the scheduler. Owner click-through required |

## Findings

### Critical (blocks)

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/components/JobApplyDialog.vue:146 | L146: 🔴 Footer Close emits `update:open(false)` directly, bypassing the DialogShell dirty guard; unsaved cover letter is discarded without a prompt. Route it through the shell's requestClose and add a close-while-dirty test. → T-022 | critic (held-out T-020), survey:frontend-B | 0.9 |

### Major

LGTM

### Minor

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/pages/jobs/applications.vue:78 | L78: 🔵 No sortable columns; outcome 3 requires sort. → T-023 | survey:frontend-A | 0.9 |
| 2 | web/pages/connections.vue:76 | L76: 🔵 Test result prints raw `ok`/`error` instead of StatusChip. → T-023 | survey:frontend-A | 0.7 |
| 3 | web/components/DomainTable.vue:168 | L168: 🔵 Mail-posture badge logic duplicated with DomainDetailDialog.vue:341 (outcome 5). Move it to utils/status.ts. → T-023 | survey:patterns | 0.9 |

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/pages/jobs/index.vue:16 | Min-score input not debounced; refetches per keystroke. → T-023 | survey:frontend-A | 0.6 |
| 2 | web/components/OAuthDialog.vue:178 | Pasted OAuth URL shown as plain text (plan-mandated by T-011); cleared on close/unmount | survey:security | 0.7 |
| 3 | web/components/ConnectionDialog.vue:267 | Cancel closes without the dirty confirm (intended as explicit discard, documented) | critic | 0.5 |

Deterministic rows: 41 selected. Clean after scoping to changed files: det-01…05, 09, 11–14, 19, test-tamper (expects 895→1195, py asserts 5086→5094), all design-* rows (no Tailwind). The det-06/07/10/15 hits were false positives: form-field defaults, error strings, early-return guards, and help text naming localhost; the `apiBase` localhost default in nuxt.config.ts predates this plan. design-raw-color/spacing and all-caps: P3 advisory (CSS var fallbacks, overline style). det-17, det-18, check:anti-mock, o2-artifact-l1l2 and fw-firestore-vue-pinia are descriptive commands that can't run (error, not pass); their intent is covered by det-03/09/10 and there is no Firestore/Pinia.

## Ratchet

| Metric | Baseline | Current | Threshold | Δ | Action |
|---|---|---|---|---|---|
| test_count (web) | 404 | 404 | ≥404 | bootstrap (was 262 at base) | baseline written |
| type_errors | 0 | 0 | 0 (absolute) | 0 | — |
| as_any_count | 0 | 0 | 0 | 0 | — |
| mocks_in_src | 0 | 0 | 0 | 0 | — |
| todo_count | 0 | 0 | 0 | 0 | — |
| lint_violations | — | — | — | no linter | not measured |
| stale_worktree_branch_count | 21 | 21 | 21 | bootstrap | prune: `/blitz:sessions worktrees --apply` |

`docs/sweeps/ratchet.json` was bootstrapped this run (first check in this repo).

## Critic

`--mode reject` verdict: **REJECT** · mode: in-Claude (opus). External panel (agy, copilot): no verdict. agy's headless run was denied a tool permission; copilot is over its monthly quota. Degraded to in-Claude only. · findings: 3

## Security posture

registry-validate: ok (97 checks) · startup-validate --strict: non-zero, 2 SCHEMA findings on blitz's own metadata (`.cc-sessions/test-journal.meta.json` and `typecheck-baseline.json` lack session_id/status). No injection. · pre-trust execution: none in project content (only the plugin's own `eval` for env indirection in critic-external.sh) · security survey: CLEAN

## Automation coverage

Deterministic gates: 5/6 (typecheck ×2, tests ×2, build; lint skipped) + ratchet clean + 21/21 tasks · critic REJECT · e2e: skipped_unavailable
Not auto-verified (human owns): architectural fit, UX correctness and look, business-logic intent, regressions in untested paths.
**Recommendation:** needs-human-review

## Before merge

1. Fix T-022: web/components/JobApplyDialog.vue:146, route the footer Close through the DialogShell dirty guard and add a close-while-dirty test.
2. Click through the console at 1440px and 375px, in light and dark mode.

## Later

1. T-023: sortable Applications table, StatusChip test result, shared mailBadges(), debounced min-score.
2. Add a linter (eslint for web/, ruff for python) so the lint gate and ratchet have data.
3. Prune the 21 build worktrees (`/blitz:sessions worktrees --apply`).
4. Restore the external critic panel: allow agy's command permission, or wait for the copilot quota to reset.
