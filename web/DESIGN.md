# SignalSlate console — design foundation

A calm ops console. Neutral surfaces, one accent colour, compact density,
status always shown as icon + label (never colour alone). This document is
the source of truth for tokens and patterns; UI code should reuse them
instead of re-deriving values.

## Palette

Accent: indigo (`#3F5EFB`). One accent, used sparingly for primary actions,
active nav state, and links — not for large surfaces.

Quasar brand tokens (set in `nuxt.config.ts` `quasar.config.brand`):

| Token | Light | Dark |
|---|---|---|
| `primary` | `#3F5EFB` | `#5B7CFF` |
| `secondary` | `#5C6470` | `#8A93A3` |
| `accent` | `#3F5EFB` | `#5B7CFF` |
| `positive` | `#1F8A5E` | `#3DBE86` |
| `negative` | `#C4392B` | `#E0574A` |
| `info` | `#3E7CB1` | `#5FA0D6` |
| `warning` | `#B8860B` | `#D9A441` |
| `dark` | `#1B1E23` | `#1B1E23` |
| `dark-page` | `#121417` | `#121417` |

Neutral surfaces:

- Light background: `#F6F7F9`, card/surface: `#FFFFFF`.
- Dark background: `#121417`, card/surface: `#1B1E23`.
- Borders replace shadows for separation: `1px solid` a low-contrast neutral
  (light `#E3E6EA`, dark `#2A2E35`) rather than heavy box-shadow.

`quasar.config.dark` is `'auto'`: the console follows the OS colour-scheme
preference. The `Dark` and `LocalStorage` plugins are enabled so a manual
override (once one exists) persists across reloads.

## Typography

- Base text size: 16px minimum (readability, accessibility floor).
- Scale: 12px (caption/meta) / 14px (secondary/table cell) / 16px (body) /
  20px (section heading) / 24px (page title).
- Font: Roboto (via `@quasar/extras` `roboto-font`), system fallback stack.

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
