---
status: active
priority: P1
created: 2026-09-22
ship: manual
---
# Management console UX overhaul: navigation, visual foundation, readable data, reliable workflows

## Goal
Make the management console (`web/`, Nuxt 3 + Quasar 2) pleasant and dependable to use. Today it has no design foundation (default Quasar, no layout, brand, dark mode or shared components) and a flat 7-tab top bar that mixes digest, setup and tool pages. It also has tabs inside pages and dialogs inside dialogs, raw ids, timestamps and jargon on screen, tables stuck at 5 rows, spinners that never stop after an error, and no protection for unsaved edits. The owner's main complaints are navigation and structure, the dated look, and clunky workflows.

## Outcomes
- Every page is reachable from a grouped left drawer: Digest (Overview, Runs, Schedule), Sources (Accounts, Collectors) and Tools (Domains, Jobs). Page sections are routes, so they can be deep-linked and the back button works. There are breadcrumbs, and the drawer overlays at 375px width.  → T-006, T-013, T-014, T-015, T-018, T-019
- Every page shares one look defined in `web/DESIGN.md`: brand colours, automatic dark mode plus a toggle, and content centred at a max width. Pages use the shared `PageHeader`, `StatusChip`, `EmptyState` and `AsyncState` components.  → T-003, T-004, T-005, T-007–T-020
- The screen shows no raw ISO timestamps, ids where names belong, JSON dumps or env-var jargon. Money, numbers and durations are formatted, and tables show 25 rows per page and can be sorted.  → T-001, T-002, T-008, T-010, T-012, T-013, T-014, T-015, T-016, T-017, T-018
- Every async action shows busy, error and retry states, so no spinner is left stuck. Destructive or external actions ask for confirmation, and forms and dialogs warn before discarding unsaved edits.  → T-005, T-007, T-009, T-012, T-019, T-020
- Each formatting, status and error helper has one shared implementation, with no copies in individual pages or components.  → T-001, T-002, T-021

## Out of scope
- New features and changes to backend behaviour. The only API change is one read-only addition: `posting_title` and `company_name` on `ApplicationOut`.
- App login or auth, the `landing/` site, and the desktop assist overlay.
- Changing the existing URLs `/history`, `/config` and `/connections`. Only their nav labels change, to Runs, Schedule and Accounts.

## Assumptions
- Existing `data-testid`s stay wherever their element survives. Tests whose assertions break because of route splits or new formatting are rewritten in the same task (owner's choice).
- The Overview page is built from the existing `/status`, `/connections` and `/collectors` endpoints, with no new endpoint.
- Dark mode follows the OS by default, and the manual override is saved in Quasar LocalStorage.

## Verification
- Run `bash /home/lasswellt/.claude/plugins/cache/blitz/blitz/3.8.1/scripts/tasks.sh verify console-ux-overhaul <id>` for each task, then `/blitz:check --scope plan console-ux-overhaul` before shipping.
- Manual: start the api and web servers. Screenshot every route at 1440px and 375px, in light and dark mode (`/blitz:browse` or Playwright). Optionally, the `blitz:design-critic` agent can score the screenshots against DESIGN.md.
- Click-through:
  1. Trigger a run from Overview, then open it in Runs.
  2. Run a collector and open its items.
  3. Add a connection.
  4. Create an application and edit its cover letter, then check that closing without saving prompts.
