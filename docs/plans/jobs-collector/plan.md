# Plan: jobs-collector

## Architecture

A stateful subsystem under `pipeline/jobs/`, built the same way as the domain collector:
- the real work runs on its own scheduler job (`jobs_refresh`) with its own tables;
- the `jobs` digest collector registered in `dispatch()` only diffs stored state into items (no network);
- the apply lifecycle mirrors `DomainPurchase`: a guarded state machine with a user-only terminal action.

Four layers:
1. **Discover**: `seeds.py` (watchlist/CSV, yc-oss, HN) and `inbox_seed.py` (job-alert emails already in `collecteditem`) create `JobCompany` rows.
2. **Resolve**: `resolve.py` runs a ladder, cheapest step first: URL pattern → verified slug probe → HTML fingerprint → JSON-LD. `research.py` (Claude `web_search`/`web_fetch`) runs only on misses, and its answer is re-verified by the deterministic adapters before it is stored.
3. **Poll + score**: `ats/<kind>.py` has one adapter per ATS behind a lazy registry, so no shared-file edits. `inventory.py` upserts postings and closes them after N misses. `scoring.py` pre-filters, then batch-scores with Claude.
4. **Apply**: `apply.py` holds playbooks and the state machine, `packet.py` generates the cover letter, answers and PDF, and `api/routers/job_apply.py` serves the profile, applications and the assist queue. The desktop runner `pipeline/jobs/assist/` (Playwright, visible Chrome, persistent profile) extracts fields, asks the server to propose values (the key stays server-side), fills, advances, shows a shadow-DOM checklist overlay, and is hard-guarded against clicking final submit.

**Chosen tradeoffs**
- ATS-direct polling over aggregator APIs. Indeed's feed is gone and LinkedIn has no API; ten ATS platforms serve public JSON (live-verified).
- Human-in-the-loop assist over auto-submit. No ATS has a candidate apply API, forms are CAPTCHA-protected, and ToS/contract exposure rules out unattended submission.
- Playwright headed runner over a Chrome extension: the owner asked for the system to open a browser, it stays in Python, and file upload is reliable. The extension remains the fallback.
- Server-side mapping endpoint over giving the runner an API key: the secret stays in one place, and the LLM can only propose values for enumerated fields.
- `fpdf2` over Chromium `page.pdf()` / WeasyPrint for the cover letter: no system libraries and no browser in the server image.

**Rejected**: LinkedIn scraping (User Agreement enforced; hiQ), keyed aggregators in v1 (unverified liveness), a stealth-plugin headless bot (a detection-evasion arms race).

## File map

| Path | Change |
|---|---|
| `requirements.txt` | + `fpdf2` (T-001) |
| `requirements-assist.txt` | new: `-r requirements.txt` + `playwright` (T-001) |
| `pipeline/db.py` | + JobCompany, JobBoard, JobPosting (T-002); + JobsProfile, AnswerBank, JobApplication (T-016) |
| `pipeline/jobs/__init__.py` | settings, slug candidates, normalization, content_hash (T-003) |
| `pipeline/jobs/ats/__init__.py` | adapter protocol, RawPosting/BoardInfo, polite HTTP, lazy registry (T-004) |
| `pipeline/jobs/ats/{greenhouse,lever}.py` | T-005 |
| `pipeline/jobs/ats/{ashby,workable}.py` | T-006 |
| `pipeline/jobs/ats/workday.py` | T-007 |
| `pipeline/jobs/ats/{smartrecruiters,rippling}.py` | T-008 |
| `pipeline/jobs/ats/{bamboohr,recruitee}.py` | T-009 |
| `pipeline/jobs/ats/{personio,jsonld}.py` | T-010 |
| `pipeline/jobs/resolve.py` | resolver ladder (T-011) |
| `pipeline/jobs/research.py` | Claude web research for misses (T-012) |
| `pipeline/jobs/seeds.py` | watchlist/CSV, yc-oss, HN importers (T-013) |
| `pipeline/jobs/inbox_seed.py` | job-alert email extraction (T-014) |
| `pipeline/jobs/inventory.py` | resolve_pending, poll_all (T-015) |
| `pipeline/jobs/scoring.py` | prefilter + JobFit scoring (T-017) |
| `api/scheduler.py` | + `jobs_refresh` job (T-018) |
| `pipeline/collectors/jobs.py`, `pipeline/collectors/__init__.py`, `pipeline/health.py` | `jobs` digest source (T-019) |
| `api/routers/jobs.py`, `api/main.py` | companies/boards/postings routes (T-020) |
| `pipeline/jobs/apply.py` | playbooks + state machine (T-021) |
| `pipeline/jobs/packet.py` | cover letter, answers, PDF (T-022) |
| `api/routers/job_apply.py`, `api/main.py` | profile/answers/applications/packet (T-023); assist queue (T-024); propose endpoint (T-030) |
| `pipeline/jobs/assist/{__init__,client}.py` | runner API client (T-025) |
| `pipeline/jobs/assist/extract.{js,py}` | field extractor (T-026) |
| `pipeline/jobs/assist/mapping.py` | deterministic + answer bank + LLM proposals, server-side (T-027) |
| `pipeline/jobs/assist/fill.py` | fill, verify, submit guard (T-028) |
| `pipeline/jobs/assist/overlay.{js,py}` | checklist panel (T-029) |
| `pipeline/jobs/assist/__main__.py` | session loop + watch mode (T-030) |
| `web/composables/useJobsApi.ts` | T-031 |
| `web/components/JobDetailDialog.vue` | T-032 |
| `web/components/JobApplyDialog.vue` | T-033 |
| `web/components/JobsProfilePanel.vue` | T-034 |
| `web/components/JobCompaniesPanel.vue` | T-035 |
| `web/pages/jobs.vue`, `web/app.vue` | page + nav (T-036) |
| `tests/conftest.py` | + `JOBS_` env prefix isolation (T-003) |
| `tests/test_*.py`, `tests/fixtures/jobs_forms/*.html`, `web/tests/*.test.ts` | per task; e2e `tests/test_jobs_e2e.py` (T-037) |
| `README.md`, `.env.example` | docs + keys (T-038) |

## Coverage

| Outcome | Schema | Logic | Scheduler/Digest | API | Web | Runner | Test |
|---|---|---|---|---|---|---|---|
| O1 Companies resolved to boards | ✓ T-002 | ✓ T-003–T-015 | ✓ T-018 | ✓ T-020 | ✓ T-031, T-035 | — | ✓ per task, T-037 |
| O2 Daily polling, lifecycle, isolated errors | ✓ T-002 | ✓ T-004–T-010, T-015 | ✓ T-018 | — | ✓ T-035 (last_error) | — | ✓ T-015, T-037 |
| O3 Fit scoring, list, digest | ✓ T-016 | ✓ T-017 | ✓ T-019 | ✓ T-020 | ✓ T-032, T-036 | — | ✓ T-017, T-019, T-037 |
| O4 Application + packet + status | ✓ T-016 | ✓ T-021, T-022 | ✓ T-019 (updates) | ✓ T-023 | ✓ T-033, T-034, T-036 | — | ✓ T-023, T-037 |
| O5 Guided assist, submit guard | ✓ T-016 | ✓ T-027 | — | ✓ T-024, T-030 | ✓ T-033 | ✓ T-001, T-025–T-030 | ✓ T-028, T-030, T-037 |

No empty cells remain. "—" means the outcome needs no artifact in that layer.

## Risks

- **Undocumented endpoints drift** (Workday CXS, Workable widget, Rippling v2, BambooHR, Recruitee): each is isolated in one adapter with its own fixture test. A failing board sets `last_error` and the UI shows it.
- **Slug-probe false positives**: T-011 requires a company name/website match before accepting a hit, stores `confidence`, and allows manual override (T-020, T-035).
- **Workday resolution**: slug probing cannot find tenant + `wdN` + site. Workday depends on the HTML fingerprint, LLM research, or a pasted careers URL.
- **Rippling descriptions**: the v2 list may lack descriptions (research open question). T-008 fetches detail if an endpoint is found, else leaves it empty, which weakens fit scoring for Rippling boards.
- **LLM cost**: bounded by caps (T-003 settings), the pre-filter (T-017) and batch sizing.
- **Prompt injection via page text**: mapping only proposes values for extracted `field_id`s (T-027), navigation is playbook-driven, and final submit is code-guarded (T-028). Covered by tests.
- **Headless Chromium in tests**: T-001 installs Chromium into the dev venv. CI without browsers would fail T-026–T-030 and T-037. Acceptable: there is no CI in the repo today (no workflow files were found).
- **Research doc corrections**: five research claims were corrected after the citation probe. None feeds a task.
- **SPIDR soft warns**: T-019 (4 files: collector + dispatch + health + test, the same shape as domain T-015) and T-026 (4 files incl. a new fixture). Accepted, and a `decision` line was logged for each. No task exceeds 5 files, and no bulk or horizontal task exists.
- **Plan size**: 38 tasks, comparable to domain-collector's 30. T-001–T-020 deliver a usable collector on their own, so the loop can ship incrementally.

## Solutions consulted

- `docs/plans/BACKLOG.md`: absent.
- `docs/solutions/`: absent.
- Precedent read directly: `docs/plans/domain-collector/{spec.md,tasks.json}`. Its task granularity, verify-command shape (grep + `venv/bin/python -m pytest` / `cd web && npx vitest run`) and router-ordering lesson (`api/main.py:105-107`) were adopted.

## Research

- `docs/_research/2026-09-21_jobs-collector.md`: ATS endpoints live-verified 2026-09-22, resolver ladder, §Apply Assist design, citation health PASS.
