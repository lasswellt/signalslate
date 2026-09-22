---
result: PASS
ts: 2026-09-22T01:51:39Z
ref: 133668f8283aad19b56126c9ac5a0bb8093d8196
scope: plan
plan: management-ui
---

# Check — management-ui (re-check)

**Result:** PASS · **Base:** origin/main · **Files:** 88 (plan-scoped; excludes the concurrent domain-collector plan sharing this branch) · **LOC:** +27312 / −303 (plan-scoped)

## Summary

Re-check after the FAIL at ref 8868e47. Tasks T-034..T-044 fixed all 8 findings that blocked the earlier run (P0 Gmail-create blocker plus 7 Majors); two independent survey lenses and the in-Claude reject critic each reproduced the fixes with real evidence, not just re-read tests. Dual external critic panel () also ran: copilot LGTM, agy could not answer (permission/tooling limitation, not a finding — see Critic section). No P0/P1 remains; 24 minor/info items from the original report stay untasked by explicit Ruling.

## Gates

| Gate | Before fix (ref 8868e47) | After fix (ref 133668f8283aad19b56126c9ac5a0bb8093d8196) | Status |
|---|---|---|---|
| pyright | 54 errors (0 in this plan's files) | 54 errors (0 in this plan's files; identical to origin/main baseline) | PASS |
| web typecheck | 0 errors | 0 errors | PASS |
| tests (python, full) | 1597/1597 | 2227/2227 (growth partly from the concurrent domain-collector plan on this branch) | PASS |
| tests (web, vitest) | 128/128 | 233 total; **229/229 pass in every file this plan touches**; 4 failures confined to `tests/domains.test.ts`, the concurrent domain-collector plan's own in-flight test file (reproduced twice, not flaky, not in this plan's scope) | PASS (plan scope) |
| build (nuxt build) | PASS | not re-run this pass (unchanged since the T-044 sweep; no plan file since then risks it) | PASS (carried) |

## Tasks (plan scope)

| Task | passes | last_verify.ok |
|---|---|---|
| T-001 … T-031, T-033 … T-044 (43 tasks) | true | true |
| T-032 | false (blocked circuit-breaker, superseded by T-033; Ruling in progress.md) | skipped, advisory only |

## Held-out checks (critic-authored, plan scope)

All 43 in-scope tasks: **43/43 ok=true** (in-Claude reject critic, real tool access, fresh commands never taken from verify[]). The load-bearing one: **T-034** — through the real app (temp DB, fresh key, CSRF header, scheduler disabled), `POST /api/connections` with `kind=gmail` and no `refresh_token` → **201**, id appears in a follow-up `GET /connections`, no secret echoed. This is the exact scenario that produced the original P0's 503. Full list: see the reject-critic transcript folded into this run; representative samples for T-035 (legacy alias no-key install: no crash, error HealthResult), T-040 (Host guard: `testserver` 200, `attacker.example` 400), T-041 (rotate under a new key, old key then unreadable, new-key-only vault still decrypts) also passed.

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| Real Google/Entra sign-in in both modes; Entra custom https redirect | manual portal steps in spec §Verification | still open, documented as manual-only |
| Web/API on one registrable site (Lax nonce cookie + credentials:include) | manual run through the real reverse-proxy topology | still open |
| Popup-blocker path for the sign-in tab | manual browser check | still open (a Reopen fallback exists) |

## Findings

### Critical (blocks)

LGTM — none.

### Major

LGTM — the 8 Majors from the prior report (findings 1-8: Gmail create, M365 alias regression, unseeded-env vanishing, callback poll false-expired, access-log leak, no Host validation, key rotation incomplete, naive timestamps) are all **confirmed fixed** by independent re-check (two survey lenses + reject critic, each with reproducing evidence). See docs/plans/management-ui/progress.md for the fix commits (T-034..T-044).

### Minor

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | pipeline/oauth_flows.py:250-252 | 🔵 OAuth flow outcome table evicts oldest-INSERTED (not oldest-unpolled) at a 100-entry cap; a flood of consumed flows could theoretically evict a legitimate unpolled outcome before the browser sees it. Requires the CSRF header to reach `consume()`, so low exploitability. Pre-existing pattern from T-037, not a regression. | survey:security-backend (re-check) | 0.4 |
| 2-15 | (carried) | The 14 Minor findings from the original report (duplicated `_coded`/`_scrub` helpers across routers, inconsistent error-body shapes, `redirect_mode` field unused, small races, a11y label duplication, docker-compose single-hostname override undocumented, etc.) are unchanged and untasked by explicit Ruling in progress.md. | carried from ref 8868e47 | — |

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 16 | .cc-sessions/typecheck-baseline.json | ❓ `startup-validate --strict` exits 2 on a SCHEMA finding for a gitignored hook-owned file (not injection); caps the posture gate at CONDITIONAL, informational only. | startup-validate | 1.0 |
| 17 | (n/a) | ❓ `.claude/settings.json` (untracked, `{"env":{"BLITZ_CRITIC_PANEL":"agy,copilot"}}`) appeared in the working tree during this run; not written by an explicit Write/Edit call in this session's own record. Left uncommitted and untouched; flagged for the owner, not treated as a finding against the plan. | manual | 1.0 |

## Auto-fix (`--fix`)

Not run.

## Ratchet

`docs/sweeps/ratchet.json` still does not exist (never bootstrapped for this repo); no regression possible, nothing written.

## Critic

**In-Claude, `--mode reject`:** verdict **LGTM**. 43/43 tasks `last_verify.ok=true`, 43/43 held-out checks pass, reject-authority detectors (det-01/02/03/04/06/07/11/13) clean, no `--no-verify` in history, no test file renamed/deleted, ratchet file absent (skipped, not a violation).

**External panel (`BLITZ_CRITIC_PANEL=agy,copilot`), `--mode pre-pass`:**
- **copilot:** verdict **LGTM** ("No reject signals found across the reject checklist"), zero findings.
  - *First attempt (transparency note):* copilot initially returned REJECT with reason "required scripts/tasks.sh is missing" — this was a **prompt-design bug on my side**, not a real finding: I had embedded the in-Claude critic's protocol verbatim, which instructs the reader to execute bash checks itself, but copilot (by this script's own design) is granted NO execution tools ("no --allow-all-tools: a critic reads and answers, it does not act"), so it could not run `scripts/tasks.sh verify` from its sandboxed prompt-only context and correctly refused to fabricate that it had. The reply had `findings: []`, `held_out: []`, `blocked_reason: dependency-missing` — i.e. it never actually reviewed the diff for a defect. I rebuilt the prompt to state explicitly that it has no tools and to supply every checklist item's result as pre-computed data instead of an instruction to execute; the re-run then produced a genuine LGTM verdict. Its `held_out[]` entries echo the pre-computed summary rather than independently constructed commands (it has no way to run anything itself), so its incremental signal beyond the in-Claude critic is a real but shallower "no defect visible in the diff text" read, not an independent re-execution.
- **agy:** **no verdict** (recorded in `errors[]`, does not block per the panel's own "any REJECT blocks" rule — a non-answer is not a REJECT). Both attempts failed identically: `jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.` Fixing this needs either `--dangerously-skip-permissions` (a real trust escalation for an external CLI acting against this checkout) or a scoped `permissions.allow` rule in agy's own settings — neither of which I applied unilaterally; flagged for the owner to decide.
- **Panel verdict:** LGTM (no provider returned REJECT; copilot answered LGTM, agy did not answer).

## Security posture

registry-validate: ok · startup-validate --strict: non-zero (SCHEMA on `.cc-sessions/typecheck-baseline.json`, not injection) · pre-trust execution: none

## Automation coverage

Deterministic gates: 5/5 applicable (pytest, pyright unchanged, web typecheck, web tests scoped to plan, ratchet n/a) · critic: in-Claude LGTM + external panel LGTM (copilot) / no-answer (agy) · e2e: skipped_unavailable (no Playwright)
Not auto-verified (human owns): real Google/Entra sign-in, reverse-proxy cookie behavior on one registrable site, popup-blocker UX, business-logic intent.
**Recommendation:** needs-human-review (manual sign-in + safety steps in spec §Verification still open; agy panel member unresolved)

## Before merge

1. Run the manual steps in spec §Verification: key on/off, Gmail and Microsoft paste-back, the automatic HTTPS callback, and a cross-origin write rejection from a browser tab.
2. Decide whether to fix the agy panel-member tooling gap (permission grant) or drop it from the panel going forward; it never blocked this run, it just never voted.
3. Decide how to publish `feat/management-ui`: the branch also carries unpushed commits from the concurrent domain-collector plan.

## Later

1. The 14 carried Minor findings (duplicated router helpers, inconsistent error shapes, small races, a11y labels, compose single-hostname doc gap).
2. `pipeline/oauth_flows.py` outcome-table eviction by oldest-inserted rather than oldest-unpolled (low severity, pre-existing).
