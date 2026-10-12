# Plan: package-tracking

## Architecture
Email-first. A `pipeline/packages/` package mirrors `pipeline/domains/` and `pipeline/jobs/`: settings in `__init__.py`, pure extraction modules (`numbers`, `html`, `extract`, `email_status`, `links`), a DB-facing `inventory`, a Gmail `scan`, carrier adapters under `carriers/`, and `poll`. A `packages_refresh` APScheduler job runs scan then poll. The API, digest collector, composable and page follow the domains feature file for file.

Tradeoff chosen: deterministic extraction (vendored jkeen formats + checksums + sender and label rules) validated on the owner's mailbox at 50/50 precision, over an LLM extractor (privacy, cost) or a single regex (8% false positives before rules). Rejected: scraping (Akamai, USPS page churn), Google search box (no API, robots.txt), aggregators (tiny free tiers), Karrio (heavy, same carrier keys), JSON-LD tier (absent in real mail), FedEx/DHL adapters (their notification mail already covers every package).

Correlation key is (carrier, tracking number), else (merchant domain, order id). Gmail `threadId` is never a key: Gmail merges unrelated same-subject Walmart mails into one thread.

## File map
- `pipeline/packages/data/` → vendored jkeen couriers JSON + LICENSE + SOURCE.md (T-001)
- `pipeline/db.py` → Package, PackageEvent, PackageEmail (T-002)
- `pipeline/packages/__init__.py` → settings, status enum (T-003)
- `pipeline/packages/numbers.py` → pattern matcher + checksum validators (T-004)
- `pipeline/packages/html.py` → link/text harvester, wrapper decoding (T-005)
- `pipeline/packages/links.py` → carrier deep links (T-006)
- `pipeline/packages/extract.py` → tiered extraction (T-007)
- `pipeline/packages/email_status.py` → status from mail (T-008)
- `pipeline/packages/inventory.py` → upsert/correlation/events (T-009)
- `pipeline/packages/scan.py` → Gmail scan + cursor (T-010)
- `pipeline/packages/carriers/{__init__,base,ups,usps}.py` → adapters (T-011..T-013)
- `pipeline/packages/poll.py` → due selection + apply (T-014)
- `api/scheduler.py` → packages_refresh job (T-015)
- `pipeline/collectors/packages.py`, `pipeline/collectors/__init__.py`, `pipeline/health.py` → digest source (T-016)
- `api/routers/packages.py`, `api/main.py` → routes incl. summary (T-017)
- `web/composables/usePackagesApi.ts` (T-018); `web/components/PackageDetailDialog.vue` (T-019); `web/components/PackageAddDialog.vue` (T-020); `web/pages/packages.vue`, `web/layouts/default.vue`, `web/tests/smoke.test.ts` (T-021)
- `tests/test_packages_*.py`, `tests/test_package_tables.py`, `tests/test_scheduler_packages.py`, `tests/test_collector_packages.py`, `tests/test_api_packages.py`, `web/tests/*Package*.test.ts` → per task
- `README.md`, `.env.example` (T-023)

## Coverage
| Outcome | Data/Schema | Extraction | Status | Scan/Poll | Scheduler | API | Web | Digest | Test |
|---|---|---|---|---|---|---|---|---|---|
| Scan creates packages | T-001, T-002, T-003 | T-004, T-005, T-007 | — | T-010 | T-015 | — | — | — | T-022 |
| Status from email | T-002 | — | T-008 | T-009, T-010 | T-015 | — | — | — | T-022 |
| UPS/USPS polling | T-003 | — | — | T-011, T-012, T-013, T-014 | T-015 | — | — | — | T-022 |
| /packages page | — | T-006 | — | — | — | T-017 | T-018, T-019, T-020, T-021 | — | T-021 |
| Digest + HA summary | — | — | — | — | — | T-017 | — | T-016 | T-022 |

## Risks
- UPS/USPS payload shapes and USPS eligibility for individuals (April 2026 access-control change) are unverified; adapters must surface "not authorized" as a carrier state, not a crash. Keys may never be obtained; the feature works without them.
- Gmail volume: Walmart alone sends ~240 shipping mails a year; scan must page and respect the existing backoff and `MAX_PAGES` guard.
- Merchant template drift (Walmart, Home Depot) breaks sender rules silently; T-022 and the fixture set catch regressions only for known shapes.
- Weak checksums (DHL Express mod 7) and checksumless formats: precision relies on the context rules from the scan; any new carrier pattern must ship with a fixture.
- SPIDR: tasks are sliced by behaviour (extraction, status, correlation, scan, polling, routes, page); T-016 touches four files but is one behaviour (register a digest source).

## Solutions consulted
- `docs/solutions/`: absent. `docs/plans/BACKLOG.md`: absent.
- `docs/plans/domain-collector/` (prior plan): reused task shapes and verify conventions (`venv/bin/python -m pytest`, `cd web && npx vitest run`), scheduler/digest/router patterns.

## Research
- `docs/_research/2026-10-10_package-tracking.md` (incl. Validation, Merchant survey, Universal-pattern scan)
- `docs/_research/assets/2026-10-10_tracking_scan.py` (reference implementation for T-004/T-005/T-007)
- `tests/fixtures/packages/extraction_cases.json` (acceptance set)
