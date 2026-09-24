---
result: FAIL
ts: 2026-09-24T01:56:18Z
ref: 531eeb2d6d5a6bdc0ca7805c5dc00f391d789c33
scope: plan
plan: console-ux-overhaul
---

# Check — console-ux-overhaul (re-check after T-022/T-023)

**Result:** FAIL · **Base:** 5cf2244 · **Files:** 73 (excl. docs/plans) · **Delta since last check:** 11 files

## Summary

T-022 (dirty-guard bypass) and T-023 (3 Minor + 1 Info) are fixed and pass their checks, and the T-020 held-out probe now passes. The reject critic found a new blocker. `/history/:id` never renders the run detail page, because Nuxt makes `pages/history/[id].vue` a child of `pages/history.vue`, which has no `<NuxtPage/>`. So the Runs row click and the Overview last-run link both show the list. The bug was already there at base, but T-008 claims list plus detail. It is added as T-024. A human still owns the visual/UX review.

## Gates

| Gate | Result | Status |
|---|---|---|
| typecheck (web nuxt typecheck / pyright changed py) | 0 / 0 errors | PASS |
| lint | no linter configured | SKIPPED |
| tests (web, full) | 471/471 | PASS |
| tests (python, full) | 2903 passed | PASS |
| build (nuxt build) | ok | PASS |

## Tasks (plan scope)

23/23 `passes: true` (T-001…T-023, re-verified this run). T-024 added (origin: check), open.

## Held-out checks (critic-authored, plan scope)

| Task | ok | Probe |
|---|---|---|
| T-001…T-007, T-009…T-023 | true | incl. T-020 `vitest -t 'unsaved cover letter'` (now passes), T-022 `defineExpose({ requestClose })` and 0 direct closes, T-023 4 sortable columns / 0 local MailBadge copies / debounce |
| T-008 | **false** | SSR `curl /history/abc123` returns the Runs list ("Every digest run, newest first"), none of the detail page's testids |

## Findings

### Critical (blocks)

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/pages/history.vue:1 | 🔴 No `<NuxtPage/>`, and `history/[id].vue` is its child route, so run detail never renders (history.vue:136, index.vue:24). Fix: move history.vue to history/index.vue. → T-024 | critic (held-out T-008) | 0.95 |

### Major

LGTM

### Minor

LGTM

### Info

| # | Where | Finding | Source |
|---|---|---|---|
| 1 | web/components/JobApplyDialog.vue:147 | The footer Close isn't visibly disabled while saving. The click is a no-op through DialogShell's busy guard. | survey:frontend |
| 2 | web/components/OAuthDialog.vue:178 | The pasted OAuth URL is shown as plain text, as T-011 specified, and is cleared on close. | survey:security |

Deterministic rows over the changed files: all clean (det-01/03/04/05/07/09/10/13/19). Test-tamper: expects went 895→1203. The 4 `expect` lines removed since the last check were replaced with equivalent StatusChip assertions (reviewed). No trivial or skip insertions.

## Ratchet

| Metric | Baseline | Current | Threshold | Action |
|---|---|---|---|---|
| test_count (web) | 404 | 407 | ≥407 | tightened, history appended |
| type_errors | 0 | 0 | 0 absolute | — |
| as_any_count / mocks_in_src / todo_count | 0 | 0 | 0 | — |
| stale_worktree_branch_count | 21 | 21 | 21 | — |

## Critic

`--mode reject` verdict: **REJECT** · mode: in-Claude (opus). External panel (agy, copilot) still unavailable: agy needs a headless tool permission and copilot is over quota. · findings: 1

## Security posture

registry-validate: ok (97 checks) · startup-validate --strict: ok after moving 2 stale blitz cache files (`test-journal.meta.json`, `typecheck-baseline.json`, both from 2026-09-22) to `.cc-sessions/quarantine/` · pre-trust execution: none in project content · security delta survey: CLEAN

## Automation coverage

Deterministic gates: 5/6 (lint skipped) + ratchet clean + 23/23 tasks · critic REJECT · e2e: skipped_unavailable (the running API would use the real `data/digest.db` and scheduler). The critic's SSR probe of the built app did exercise routing.
Not auto-verified (human owns): architectural fit, UX correctness and look, business intent, regressions in untested paths.
**Recommendation:** needs-human-review

## Before merge

1. T-024: `git mv web/pages/history.vue web/pages/history/index.vue` and update the import in `web/tests/history.test.ts`.
2. Click through the console at 1440px and 375px, in light and dark mode.

## Later

1. Add a route-level test that navigates to `/history/:id` through the router, not by mounting the page directly.
2. Add linters (eslint for web, ruff for python).
3. Prune 21 build worktrees (`/blitz:sessions worktrees --apply`).
