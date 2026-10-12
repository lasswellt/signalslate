# Plan: console-ux-overhaul

## Architecture
- **Helpers.** `web/utils/` is auto-imported by Nuxt and holds three files:
  - `format.ts`: `formatDate`, `relativeTime`, `formatDuration`, `formatMoney` (built on Intl), `formatNumber` and `humanize`.
  - `status.ts`: one map, `statusMeta(kind, value) → {color, icon, label}`. `partial` is warning-coloured everywhere.
  - `errors.ts`: `errorText`.
- **Shared components** in `web/components/ui/`:
  - `PageHeader`: title, subtitle and an actions slot.
  - `StatusChip`: always shows an icon and a label, never colour alone.
  - `EmptyState`
  - `AsyncState`: skeleton, error with retry, empty state, or the default slot.
  - `DialogShell`: `maximized` below the `sm` breakpoint, and asks for confirmation on close when `dirty`.
- **Shell.** `web/layouts/default.vue` holds a `q-drawer` (a mini rail at md and wider, an overlay below that) with nav links grouped by section, plus breadcrumbs taken from the route and a dark-mode toggle. `app.vue` is reduced to `<NuxtLayout><NuxtPage/></NuxtLayout>`.
- **Theme.**
  - In `nuxt.config.ts`: `quasar.config.brand` and `dark: 'auto'`, with the `Dark` and `LocalStorage` plugins.
  - In `web/assets/css/app.scss`: surface tokens, a `.page-container` class with a max width, and dense table defaults.
- **Routes.**
  - `pages/domains.vue` and `pages/jobs.vue` become parent routes that render `q-route-tab`s and `<NuxtPage/>`. Their children:
    - `domains/{index,watchlist,ideas,purchases}`
    - `jobs/{index,applications,companies,profile}`
  - The collector items dialog becomes the `collectors/[source]` route.
- **API.** `ApplicationOut` gains `posting_title` and `company_name`, filled by a join (`api/routers/job_apply.py:432`).
- **Rejected alternatives.**
  - A grouped top bar: the owner rejected it.
  - Joining application names on the client: this would mean fetching every posting.
  - A new `/overview` endpoint: not needed, since the existing endpoints cover it.

## File map
- `web/utils/{format,status,errors}.ts`: new shared helpers.
- `web/components/ui/{PageHeader,StatusChip,EmptyState,AsyncState,DialogShell}.vue`: new.
- `web/layouts/default.vue`: new app shell.
- `web/app.vue`: reduced to NuxtLayout, which also removes the malformed tab on line 12.
- `web/DESIGN.md`, `web/assets/css/app.scss` and `web/nuxt.config.ts`: theme.
- `web/pages/index.vue` and `web/components/HealthList.vue`: Overview.
- `web/pages/history.vue` and `web/pages/history/[id].vue`: Runs.
- `web/pages/config.vue`: Schedule.
- `web/pages/connections.vue`, `web/components/{ConnectionDialog,OAuthDialog}.vue`: Accounts.
- `web/pages/collectors.vue` and `web/pages/collectors/[source].vue`: Collectors.
- `web/pages/domains.vue`, `web/pages/domains/*.vue` and `web/components/{DomainTable,DomainIdeasPanel,DomainPurchaseDialog,DomainDetailDialog}.vue`: Domains.
- `web/pages/jobs.vue`, `web/pages/jobs/*.vue` and `web/components/{JobCompaniesPanel,JobsProfilePanel,JobApplyDialog,JobDetailDialog}.vue`: Jobs.
- `api/routers/job_apply.py`, `tests/test_api_job_apply.py` and `web/composables/useJobsApi.ts`: the application name join.

## Coverage
| Outcome | Helpers | UI kit | Shell/theme | Pages | API | Tests |
|---|---|---|---|---|---|---|
| Grouped nav + routes | — | — | ✓ T-006 | ✓ T-013 T-014 T-015 T-018 T-019 | — | ✓ per task |
| Consistent look | — | ✓ T-004 T-005 | ✓ T-003 | ✓ T-007–T-020 | — | ✓ per task |
| Readable data | ✓ T-001 T-002 | ✓ T-004 | — | ✓ T-008 T-010 T-012–T-018 | ✓ T-017 | ✓ per task |
| Reliable workflows | ✓ T-002 | ✓ T-005 | — | ✓ T-007 T-009 T-012 T-019 T-020 | — | ✓ per task |
| One helper copy | ✓ T-001 T-002 | — | — | — | — | ✓ T-021 |

## Risks
- **Route splits break page tests.** Tests that mount `pages/jobs.vue` or `pages/domains.vue` directly must be rewritten to mount the child pages. This work is inside T-014, T-015, T-018 and T-019.
- **Dark mode in tests.** `dark: 'auto'` may need `matchMedia` stubbed under happy-dom; handle this in T-003.
- **Oversized tasks.** T-014, T-015 and T-019 touch 4–5 small files. They are split by route, not by file (SPIDR check), because each moves existing template code rather than adding new logic.
- **Weak verify check.** T-009's third check (tracker labels) is weak. The vitest run and the PageHeader grep carry that task.

## Solutions consulted
- `docs/solutions/` absent; `docs/plans/BACKLOG.md` absent. No effect.

## Research
- Two in-session read-only UX audits (Explore agents) of all 7 pages, 9 components and 3 composables. Findings are cited file:line in task `--notes` and summarised in spec.md §Goal.
