# SignalSlate console — design foundation

A calm ops console. Neutral surfaces, one accent colour, compact density,
status always shown as icon + label (never colour alone). This document is
the source of truth for tokens and patterns; UI code should reuse them
instead of re-deriving values.

## Palette

**Slate & Tide**: tinted slate neutrals, an ink primary, and a sea-teal accent
reserved for signal/interactive chrome (focus, active nav, links, selection,
"new" markers) — never on status chips or badges.

CSS custom properties (`--ss-*`, defined in `assets/css/app.scss`, keyed on
`body.body--light` / `body.body--dark`):

| Token | Light | Dark | Role |
|---|---|---|---|
| `--ss-bg` | `#F5F7F7` | `#0E1415` | page |
| `--ss-surface` | `#FFFFFF` | `#151D1F` | cards, header |
| `--ss-surface-2` | `#EEF2F2` | `#1C2628` | table header, hover |
| `--ss-border` | `#DCE3E3` | `#27322F` | 1px separation (borders not shadows) |
| `--ss-text` | `#111718` | `#E2EAEA` | body |
| `--ss-muted` | `#526265` | `#8C9D9F` | secondary text (≥5.4:1) |
| `--ss-primary` | `#18201F` | `#E2EAEA` | primary buttons (ink; inverts in dark) |
| `--ss-on-primary` | `#FFFFFF` | `#0E1415` | text on primary |
| `--ss-accent` | `#1B6668` | `#5DBDB9` | focus ring, active nav, links, selection, "new" dot — never on status |
| `--ss-accent-subtle` | `#E2EFEE` | `#173335` | selected row, accent chip bg |
| `--ss-link` | `#1B6668` | `#7FCFCB` | inline links (underlined) |

Quasar brand tokens: `quasar.config.brand` (in `nuxt.config.ts`) only holds
one set of values, so it carries the **light** palette; the **dark** palette
is applied at runtime via `--q-*` overrides in `assets/css/app.scss` under
`body.body--dark`:

| Quasar name | Light (`quasar.config.brand`) | Dark (`--q-*` override) |
|---|---|---|
| `primary` | `#18201F` | `#E2EAEA` |
| `accent` | `#1B6668` | `#5DBDB9` |
| `positive` | `#1B7A4B` (✓ Success) | `#5CC98F` |
| `negative` | `#B3261E` (✕ Failed) | `#F2796D` |
| `warning` | `#8A5A00` (! Partial) | `#E3A63B` |
| `info` | `#4B55B5` (◷ Running) | `#9AA4FF` |
| `dark` / `dark-page` | `#151D1F` / `#0E1415` | (same — dark surfaces) |

Logo wave: `#4DB3AF` on a `#111718` tile (light), `#5DBDB9` on a `#1C2628`
tile (dark).

`quasar.config.dark` is `'auto'`: the console follows the OS colour-scheme
preference. The `Dark` and `LocalStorage` plugins are enabled so a manual
override (once one exists) persists across reloads.

## Typography

- Base text size: 16px minimum (readability, accessibility floor).
- Scale: 12px (caption/meta) / 14px (secondary/table cell) / 16px (body) /
  20px (section heading) / 24px (page title).
- UI font: **Instrument Sans Variable** (wght 400–700), self-hosted via
  `@fontsource-variable/instrument-sans` and wired through
  `$typography-font-family` in `quasar.variables.scss` — no runtime CDN
  fetch. 400 for body text, 500 for labels/buttons, 600 for headings.
  Headings carry `letter-spacing: -0.015em`.
- Mono font: **JetBrains Mono Variable**, self-hosted the same way, applied
  via the `.mono` utility class. Used for IDs, run numbers, JSON, and logs;
  `font-feature-settings: "zero" 1` for a slashed zero.
- Tabular numerals: `font-variant-numeric: tabular-nums` on `.q-table td`,
  `time`, and `.num` so numeric columns and counts align.
- Digest face (reserved, Phase 4 — not bundled in `web/`): **Source Serif 4**
  for the printed/e-ink digest body copy; not part of the console's web
  bundle today.

## Usage rules

- Primary actions use `primary` (ink) — buttons, main CTAs.
- Accent (`--ss-accent` / Quasar `accent`) is reserved for signal/interactive
  chrome: focus rings, active nav indicator, links, selection, "new" markers.
  **Accent is never used on status chips or badges.**
- Status is always icon + label, never colour alone (see Status chips below).
- Digest / e-ink status (Phase 4) is glyph + weight only, never colour: ✓
  Success, ✕ Failed, ! Partial, ◷ Running, rendered at emphasis weight
  650–700 rather than a colour change.
- The contrast guard in `web/tests/design-tokens.test.ts` must stay green —
  re-run it whenever a hex value in this document or `app.scss` changes.

## Brand assets

- `web/public/logo.svg` — full logo mark.
- `web/public/favicon.svg` — 16px-simplified favicon.
- `web/public/logo-lockup.svg` / `logo-lockup-dark.svg` — outlined Instrument
  Sans SemiBold wordmark lockups for light/dark surfaces.
- Wave colour: `#4DB3AF` (light tile) / `#5DBDB9` (dark tile).

## Spacing

8px grid: 4, 8, 16, 24, 32, 48px steps. Card padding 16px. Section gaps 24px.

## Density

Compact by default:

- Tables use Quasar's `dense` prop; default page size 25 rows.
- Form fields use `dense` where the Quasar component supports it.
- Dialogs avoid excess whitespace; content max-width matches the page
  container (see below) rather than stretching full viewport.

## Status chips

Every status indicator is icon + label, never colour alone:

```html
<q-chip dense :color="statusColor" text-color="white" icon="check_circle">
  Active
</q-chip>
```

Pick icons that reinforce the label semantically (`check_circle` for
success/active, `error` for failure, `schedule` for pending/queued,
`pause_circle` for paused) so the state reads correctly in greyscale.

## Page anatomy

- `PageHeader` at the top of every page: title, optional subtitle/description,
  optional actions slot on the right.
- Content below the header lives in `.page-container`: `max-width: 1200px`,
  centered (`margin-inline: auto`), `16px` gutter on mobile
  (`padding-inline: 16px`), widening to 24px at `sm+`.

```html
<PageHeader title="Domains" />
<div class="page-container">
  <!-- page content -->
</div>
```

## Dialogs

- Shared `DialogShell` wraps `q-dialog` content: consistent header (title +
  close button), body, and footer action row.
- Dialogs are `maximized` below the `sm` breakpoint (<600px) so forms are
  usable on mobile, and a fixed-width card (typically 480–640px) at `sm+`.

## Empty / loading / error states

Every data-driven view implements all three:

- **Loading**: `q-skeleton` placeholders shaped like the eventual content
  (text/rect for cards, `type="QTable"`-like rows for tables) — never a bare
  spinner for primary content.
- **Empty**: an icon, a short plain-language message, and — where relevant —
  a primary action ("No domains yet. Add your first domain.").
- **Error**: the error message plus a retry action (`q-btn` calling the
  fetch again). No raw stack traces or exception class names in the copy.

## Accessibility

- Icon-only buttons always carry `aria-label` and a `q-tooltip` with the same
  text.
- Every `q-input`/`q-select`/form control has a visible `label` prop or an
  associated `<label>`.
- Status is never colour-only — always paired with an icon and text label.
- Interactive elements are reachable by keyboard (native `q-btn`/`q-item`
  focus order follows visual/DOM order; no `tabindex` traps).
- Touch targets are at least 44×44px; base text is at least 16px.
- Tables either scroll horizontally within their container on narrow
  viewports or collapse to a card list — never force page-level horizontal
  scroll.

## Copy tone

Plain language throughout:

- No raw database ids in labels — show the human-readable name/title, keep
  ids in tooltips or dev-only detail views if needed at all.
- No raw ISO timestamps in primary copy — format relative or localized
  ("2 hours ago", "Sep 22, 2026") and reserve the raw ISO string for a
  tooltip/title attribute.
- No environment-variable names or internal jargon ("watermark", "digest
  path") in user-facing copy — describe what happened in plain terms instead.
