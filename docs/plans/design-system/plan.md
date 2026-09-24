# Plan — design-system

## Architecture

The theme is re-done in two layers, following the research doc's §Implementation Sketch.

1. **Semantic CSS tokens** (`--ss-*`) in `web/assets/css/app.scss`, keyed on Quasar's `body.body--light` / `body.body--dark`. The dark block also overrides Quasar's `--q-*` brand variables. That is the only way to get distinct dark brand colours, because `quasar.config.brand` in `nuxt.config.ts` holds a single set.
2. **Quasar brand hexes** in `nuxt.config.ts`. Because `web/utils/status.ts` and all 147 colour-class usages resolve through semantic brand names, this re-themes status chips, buttons and notifications without touching components.

**Fonts:** `@fontsource-variable` packages, imported through `nuxt.config.ts` `css`, bundled by Vite (LAN-only, no CDN). The Quasar typography font comes from a `sassVariables` file.

**Tradeoffs:**
- **Chosen:** CSS variables + Quasar brand. Minimal diff, uses the existing semantic indirection.
- **Rejected:** a Tailwind/UnoCSS token system. It adds a second styling system beside Quasar.
- **Rejected:** full Radix-style 12-step OKLCH scales. Nothing consumes intermediate steps yet, so they'd be unused tokens. The research recommends the structure; this plan lands the semantic layer only.
- **Rejected:** `@nuxt/fonts`. Its default provider fetches from Google at build time and would need extra config to stay local, whereas fontsource is plain npm.

## File map

| Path | Change |
|---|---|
| `web/assets/css/app.scss` | new `--ss-*` token set, light and dark; dark `--q-*` overrides; `tabular-nums` + `.mono` utilities (T-001, T-004) |
| `web/nuxt.config.ts` | brand hexes → Slate & Tide (T-002); drop `roboto-font`, add fontsource css, `sassVariables` (T-004) |
| `web/tests/design-tokens.test.ts` | new: WCAG AA guard over brand and dark tokens (T-002) |
| `web/pages/config.vue` | `var(--ss-surface-bg, #f6f7f9)` → `var(--ss-surface-2)`, drop hex fallbacks (T-003) |
| `web/components/JobsProfilePanel.vue` | same migration (T-003) |
| `web/package.json` (+ lock) | `@fontsource-variable/instrument-sans`, `@fontsource-variable/jetbrains-mono` (T-004) |
| `web/assets/css/quasar.variables.scss` | new: `$typography-font-family` (T-004) |
| `web/layouts/default.vue` | header → surface + border, accent active nav, readable breadcrumbs (T-005) |
| `web/public/logo.svg`, `favicon.svg`, `logo-lockup.svg`, `logo-lockup-dark.svg` + `favicon.ico`, `apple-touch-icon.png`, `logo-512.png` | wave → Tide, greys → new muted, re-rasterise (T-006) |
| `web/DESIGN.md` | palette, typography and usage rules rewritten (T-007) |

## Coverage

| Outcome | Tokens (css) | Brand (config) | Consumers | Fonts | Layout | Assets | Docs | Test |
|---|---|---|---|---|---|---|---|---|
| Light + dark tokens applied | ✓ T-001 | ✓ T-002 | ✓ T-003 | — | — | — | — | ✓ T-008 |
| AA-passing status colours | ✓ T-001 | ✓ T-002 | — | — | — | — | — | ✓ T-002 |
| No old tokens or inline hex | ✓ T-001 | — | ✓ T-003 | — | — | — | — | ✓ T-003 |
| Local Instrument Sans / JetBrains Mono, tabular numbers | ✓ T-004 | ✓ T-004 | — | ✓ T-004 | — | — | — | ✓ T-004 (build) |
| Calm header + accent nav | — | — | — | — | ✓ T-005 | — | — | ✓ T-005 |
| Tide logo/favicon | — | — | — | — | — | ✓ T-006 | — | ✓ T-006 |
| DESIGN.md is source of truth | — | — | — | — | — | — | ✓ T-007 | ✓ T-007 |
| Suite + build green | — | — | — | — | — | — | — | ✓ T-008 |

No gaps.

## Risks

- **`nuxt.config.ts` touched by T-002 and T-004.** They don't depend on each other (`tasks.sh set` can't add an edge after `add`). Run them sequentially, or let `build --parallel`'s merge-tree pre-check split them into separate waves. Both edit different keys (`brand` vs `extras`/`css`/`sassVariables`), so a textual merge should be clean.
- **Ink primary is global.** 66 `primary` usages turn from indigo to ink. Buttons are intended to. `text-primary` links and icons become ink rather than teal, which is acceptable (ink is readable) but loses accent. A follow-up could move link-like usages to `text-accent`. T-008 guards regressions only, not taste.
- **Uncommitted prerequisites.** The `web/public/` logo set and README header from this session are untracked. Commit them before `/blitz:build` so worktree-isolated dev agents can see them (T-006 depends on their existence).
- **Build-time verify is slow.** T-004 and T-008 run `npm run build` (600 s timeout). Acceptable given ~8 tasks.
- **Research gaps carried over.** The Paper Pro colour e-ink treatment is untested, and "paper off-white vs white" is judgement, not evidence (see the research doc's §Open Questions). Neither blocks this plan.
- **Simulated persona panel.** The palette choice rests on a structured heuristic review, not real users. Validate on the actual console after T-005/T-006.
- **SPIDR:** the tasks slice by behaviour (tokens → brand + guard → consumers → fonts → header → assets → docs → gate), not by file. T-003 and T-006 are multi-file but single-behaviour.

## Solutions consulted

- `docs/solutions/`: absent; nothing consulted.
- `docs/plans/BACKLOG.md`: absent; no backlog lines absorbed.

## Research

- `docs/_research/2026-09-23_design-system.md` (`--from-research`; the citation critic's issue was fixed; 13 LIVE / 2 UNKNOWN)
- Specimens: `docs/_research/assets/2026-09-23_design-system/`
