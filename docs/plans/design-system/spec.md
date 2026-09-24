---
status: active
priority: P2
created: 2026-09-23
ship: manual
---
# Design system — Slate & Tide

## Goal
Replace the console's indigo-on-slate + Roboto look with the researched **Slate & Tide** system:
- tinted slate neutrals and ink primary actions;
- a deep sea-teal accent reserved for signal chrome (logo wave, focus, links, active nav);
- a self-hosted Instrument Sans / JetBrains Mono type stack.

Along the way, fix two defects the research measured: status colours that fail WCAG AA as chip text, and dark-mode brand pairs that are documented but never applied. Source: `docs/_research/2026-09-23_design-system.md`.

## Outcomes
- The console renders Slate & Tide tokens in light mode, and dark mode applies its own brand pairs (primary, accent, status) instead of reusing light hexes. → T-001, T-002
- Every status and secondary brand colour passes WCAG 2 AA (≥4.5:1) as text on its surface in both modes, enforced by a test that fails on regression. → T-002
- No stylesheet or component references the old token names or inline surface hexes. → T-003
- UI text is Instrument Sans and code/IDs can use JetBrains Mono, both bundled locally. The production build contains no Google Fonts URL and no Roboto. Numeric table columns use tabular figures. → T-004
- The header is a calm surface bar, not an ink block, and the active nav shows the Tide accent. → T-005
- Logo, favicon and lockups use the Tide wave; raster icons are regenerated from the SVGs. → T-006
- `web/DESIGN.md` documents the tokens, type roles and usage rules: accent never on status chips, status = icon + label, digest = glyph + weight. → T-007
- The full web suite and production build stay green. → T-008

## Out of scope
- Phase 4 digest PDF renderer and its Source Serif 4 embedding. The rules are recorded in DESIGN.md (T-007) and the renderer is built in Phase 4.
- Restyling individual pages or components beyond token and header changes.
- A bespoke wordmark redraw (Elise's dissent in the research doc).
- The reMarkable Paper Pro colour e-ink treatment.

## Assumptions
- `--autonomous`: no interview. Approach = the research doc's §Recommendation verbatim (fewest new files that meets every outcome).
- The logo/favicon/lockup files in `web/public/` and the README header are uncommitted work from this session. They must be committed (or present in the build worktree) before T-006 runs; a worktree cut from `main` without them will fail T-006.
- Header choice: surface bar rather than an ink bar (the research doc lists this as an open taste question; surface matches the "calm" personality and avoids a near-black header).
- Priority P2 (polish; nothing is broken in production). The AA failure is real but low-severity for a single-user LAN tool.
- `@fontsource-variable/*` 5.3.0 packages exist on npm (checked 2026-09-23). The `nuxt-quasar-ui` `sassVariables` option exists (`node_modules/nuxt-quasar-ui/dist/module.d.mts:35`).
- The 66 existing `color="primary"` / `text-primary` / `bg-primary` usages are meant to become ink. No per-usage audit is planned beyond the header (T-005) and the suite (T-008).

## Verification
- `bash /home/lasswellt/.claude/plugins/cache/blitz/blitz/3.9.3/scripts/tasks.sh verify design-system <id>` per task; run `/blitz:check --scope plan design-system` before shipping.
- Manual check: open the console in light and dark mode, and confirm the header, a status chip row, focus ring and favicon against `docs/_research/assets/2026-09-23_design-system/final.png`.
