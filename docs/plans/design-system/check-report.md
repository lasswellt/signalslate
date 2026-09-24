---
result: CONDITIONAL
ts: 2026-09-24T04:52:00Z
ref: 4999f249702c18a99fe1e7ba63cb5e5f7b10582f
scope: plan
plan: design-system
---

# Check — design-system

**Result:** CONDITIONAL · **Base:** 3b78d62 · **Files:** 16 (web/, lockfile excluded) · **LOC:** +418 / −63

## Summary

The Slate & Tide re-theme is delivered:
- tokens in both modes, brand colours, self-hosted Instrument Sans / JetBrains Mono, calm header, Tide brand assets and DESIGN.md;
- 8/8 tasks pass and all 8 critic held-out checks pass;
- the final-diff critic returned LGTM.

The `--fix` pass found and fixed one dark-mode regression (`cd7ca90`): the primary checkbox tick was 1.22:1 and is now 15.22:1. The verdict is **CONDITIONAL, not PASS**, only because the security-posture gate is non-zero. Both causes are inside the blitz plugin, not the project, and neither is an injection (see §Security posture). A human still owns the visual review with real data; the API was unreachable during every render.

## Gates

| Gate | Before fix | After fix | Status |
|---|---|---|---|
| typecheck (`cd web && npx nuxt typecheck`) | 0 errors | 0 errors | PASS |
| lint | — | — | SKIPPED (no ESLint configured in web/) |
| tests (selected) | not calibrated: `test-selector.sh` returned no files | — | calibration |
| tests (full, `cd web && npx vitest run`) | 489/489 · escaped 0 | 490/490 | PASS |
| build (`cd web && npm run build`) | PASS | PASS | PASS |
| typecheck:python-pyright (toolchain lane) | — | — | SKIPPED: no `.py` in the plan's changed set |

## Tasks (plan scope)

| Task | passes | last_verify.ok | failed | tail |
|---|---|---|---|---|
| T-001 | true | true | — | — |
| T-002 | true | true | — | — |
| T-003 | true | true | — | — |
| T-004 | true | true | — | — |
| T-005 | true | true | — | — |
| T-006 | true | true | — | — |
| T-007 | true | true | — | — |
| T-008 | true | true | — | — |

`check:test-tamper`: no deleted `expect(`, no `.skip`/`.only`, assertion count did not fall. The row exits 1 on its trailing `[ -lt ]` test with empty output, which is a clean result.

## Held-out checks (critic-authored, plan scope)

| Task | ok | Command | Tail |
|---|---|---|---|
| T-001 | true | node: compare `--ss-*` keys in the body--light and body--dark blocks | light 15 · dark 15 · gap [] |
| T-002 | true | node: WCAG light status/secondary on `--ss-bg`; dark secondary/link/accent on surfaces | min 4.97 (light positive on #f5f7f7) |
| T-003 | true | every `var(--ss-*)` used in pages/components/layouts/assets is defined | no undefined tokens |
| T-004 | true | inspect `.output`: fontsource woff2 bundled, body font-family, no Roboto | 7 font assets · `body{font-family:Instrument Sans Variable,…}` · 0 Roboto |
| T-005 | true | grep built CSS for `.ss-header` / `.ss-nav-active` | scoped rules present, accent inset indicator |
| T-006 | true | magick histogram of logo-512 / apple-touch / favicon.ico | #4DB3AF cluster, no #3F5EFB |
| T-007 | true | every hex in DESIGN.md cross-checked against the code, both directions | only doc-only hex is #4DB3AF (logo wave, expected) |
| T-008 | true | serve `.output`, curl `/` | 200 · Instrument Sans · accent tokens inlined · no fonts.googleapis |
| T-001 (post-fix) | true | sass + quasar.css harness with the real checkbox DOM in headless Chrome | dark truthy/indet tick rgb(14,20,21) on rgb(226,234,234) · light unchanged |

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| Is the focus-ring gap intentional (some global style already uses `--ss-accent`)? | grep for a global focus style | Ran `grep -rnE "focus-visible\|:focus\|q-focus" web/assets web/layouts web/app.vue`: nothing matches. The gap is real; Quasar's default focus helper applies. Carried as advisory Minor #1. |

## Findings

### Critical (blocks)

LGTM

### Major

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/assets/css/app.scss:57 | Dark checkbox tick #fff on `--q-primary` #E2EAEA (1.22:1): the mark was nearly invisible. **Fixed in `cd7ca90`** and re-measured at 15.22:1; a guard test was added. | critic (advisory) → check --fix | 1.0 (measured) |

### Minor

These are advisory: all are below the 0.8 confidence bar and none flips the verdict.

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/assets/css/app.scss (no rule); web/DESIGN.md:27,100 | 🔵 DESIGN.md names `--ss-accent` as the focus-ring colour, but no `:focus-visible` rule sets it. Add an accent outline rule, or correct the doc. | survey:frontend | 0.6 |
| 2 | web/nuxt.config.ts:26-28 vs web/assets/css/app.scss:11,13 | 🔵 Primary and accent hexes are duplicated between the Quasar brand config and the `--ss-*` tokens, with no equality assertion. They can drift silently. Add a test that asserts the brand hex equals the light `--ss-*` hex. | survey:patterns | 0.75 |
| 3 | web/assets/css/app.scss:81-95 | 🔵 In dark mode the `.text-positive/negative/warning/info` overrides also repaint currentColor **fills**. A future checkbox, toggle or radio with a status `color=` would draw a white mark at about 2:1. No current usage (11 checkbox/radio/toggle usages checked, none set a status colour). The code comment now says so. | critic (advisory) | 0.9 (measured, latent) |

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | web/tests/design-tokens.test.ts | The guard does not cover accent/link contrast, dark secondary as text, or status on light `--ss-bg`. All pass when computed by hand (6.67 / 7.70 / 6.06 / ≥4.97). | survey:patterns, critic | 0.5 |
| 2 | web/tests/design-tokens.test.ts | Regex parsing throws on a reformat rather than passing vacuously, so it fails loudly. | survey:patterns | 0.55 |

Deterministic-lane hits were FP-verified and dropped. Each dropped hit, with its evidence:
- `det-10` (`web/layouts/default.vue:87`), `det-15` (`web/nuxt.config.ts:41`), `design-all-caps-body-static` (`app.scss:103`), `design-raw-spacing` (config.vue:312-314, JobsProfilePanel.vue:793-794): lines that predate the plan (`git blame` shows commits from before 3b78d62).
- `design-raw-color-literal`: every hit is a custom-property definition in the token file.
- `design-md3-elevation-conformance`: this row belongs to the tailwind-md3 adapter, and the project uses Quasar.
- `design-extreme-negative-tracking-static`: detector regex bug. Its pattern `-(0?\.0[5-9]|[0-9])` matches the `-0` in `-0.015em`, which is not extreme tracking.

## Auto-fix (`--fix`)

| Category | Found | Fixed | Remaining | Skipped |
|---|---|---|---|---|
| Imports / exports | 0 | 0 | 0 | 0 |
| Type errors | 0 | 0 | 0 | 0 |
| Lint | — | — | — | skipped (no linter) |
| Framework (F5/V3/P2) | 0 | 0 | 0 | 0 (Vue G1-G5 on added lines: 0 hits; no Firestore/Pinia) |
| Naming / return types | 0 | 0 | 0 | 0 |
| Unused | 0 | 0 | 0 | 0 |
| Rendered contrast (critic-found regression) | 1 | 1 | 0 | 0 |

Commits: `cd7ca90 fix(check): dark-mode primary checkbox tick drawn in on-primary ink`. After the fix, gates were re-run (490/490, typecheck 0, build ok) and the critic re-ran on the final diff (LGTM).

## Ratchet

| Metric | Baseline | Current | Threshold | Δ | Action |
|---|---|---|---|---|---|
| test_count | 407 | 414 | min 414 | ↑ | tightened, history appended |
| type_errors | 0 | 0 | 0 (absolute) | 0 | — |
| as_any_count | 0 | 0 | max 0 | 0 | — |
| mocks_in_src | 0 | 0 | max 0 | 0 | — |
| todo_count | 0 | 0 | max 0 | 0 | — |
| stale_worktree_branch_count | 21 | 3 | max 3 | ↓ | tightened |
| lint_violations / completeness_score | null | null | — | — | not tracked (no linter) |

## Critic

`--mode reject` verdict: **LGTM** (run twice: pre-fix at HEAD~1 and on the final diff at `cd7ca90`) · mode: in-Claude (opus) · findings: 3 advisory (1 fixed, 2 carried above)

## Security posture

- registry-validate: ok
- startup-validate --strict: **non-zero (2)**, SCHEMA only, not injection. `.cc-sessions/test-journal.meta.json` is "missing session_id/status". That file is written by blitz's own `scripts/test-listener.sh` in its normal shape (`escaped_failures_recent`, `last_run_id`, `runs_*`); the validator expects the session-record shape. This is a plugin inconsistency, not project content.
- pre-trust execution grep: 1 hit, FP-verified. `hooks/scripts/critic-external.sh:237` `eval "_raw=\${$1:-}"` is an indirect expansion of a hard-coded env-var name inside the plugin, and does not touch project content.
- The security survey found nothing: no new network fetch at runtime, the fontsource packages have no install scripts reaching the build, and the SVGs have no scripts or external refs.

→ Per the gate rules, a non-zero posture without injection caps the verdict at **CONDITIONAL**.

## Automation coverage

- **Deterministic gates:** 6/6 that ran passed (typecheck, tests, build, ratchet, registry rows after FP-verify, critic) · lint skipped · python lane out of scope.
- **Registry lane:** 41 rows selected. 8 errored as written (5 hard-code `src/`, 2 contain prose instead of a command, 1 is a loop metric) and were re-run or covered manually over the plan's files.
- **e2e:** partial. Playwright MCP is unavailable (`/usr/bin/chromium` missing), so Chrome DevTools was used instead: 8 routes × light/dark, 0 console errors apart from API-unreachable noise, fonts applied, computed contrast of rendered fills measured. No real data was loaded because the API was down.
- **Not auto-verified (a human owns these):** architectural fit, visual/UX taste of the palette with real data, business intent, regressions in untested paths.
- **Recommendation:** needs-human-review

## Before merge

1. Open the console against a live API in both modes and look at the runs table, status chips, the domain-ideas checkboxes and a focused input (the plan's manual verification step).

## Later

1. Add a `:focus-visible` outline using `--ss-accent` (web/assets/css/app.scss), or drop the focus-ring claim from web/DESIGN.md:27,100.
2. In web/tests/design-tokens.test.ts, assert that `quasar.config.brand` primary/accent/status hexes equal the light `--ss-*` hexes in app.scss.
3. Scope the dark `.text-*` overrides so they don't repaint status-coloured checkbox/toggle/radio fills, before any of those components gets a status `color=`.
4. Blitz plugin: the `startup-validate` schema rule contradicts `test-listener.sh`'s journal shape; `next-state.sh:82` hits ARG_MAX; registry rows det-05/07/09/10/15 hard-code `src/`; `check:anti-mock` and `fw-firestore-vue-pinia` have prose `detection.command`s; `design-extreme-negative-tracking-static`'s regex matches `-0.0x`.
