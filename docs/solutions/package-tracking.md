---
title: New feature modules must register config prefixes and source lists; scheduled polls need a due tolerance; date-only ETAs are calendar dates
date: 2026-10-11
tags: [config, scheduler, timezones, carrier-api, gmail, test-expectations]
stack: [python, fastapi, sqlmodel, apscheduler, nuxt, quasar]
files: [pipeline/health.py, pipeline/packages/poll.py, api/routers/packages.py, pipeline/packages/carriers/ups.py, pipeline/packages/inventory.py, pipeline/packages/numbers.py, tests/conftest.py, .gitignore, web/utils/status.ts]
symptoms: ["env var set in container but setting stays default", "known_sources assertion: Left contains one more item", "package polled every 4h instead of 2h", "arriving today counted a day early", "UPS event times off by hours", "vendored data directory missing from commit"]
plan: package-tracking
---

## Symptom
- PACKAGES_/UPS_/USPS_ (and pre-existing DOMAINS_/JOBS_) variables set in the process environment were ignored; only .env values applied.
- Adding the always-declared `packages` digest source failed ~9 unrelated tests that pin the exact source list.
- Critic held-out test: with a `23 */2 * * *` cron, a package polled at 12:23:40 was skipped at 14:23:10.
- Home Assistant summary counted tomorrow's ETA as "arriving today" in US zones.
- `pipeline/packages/data/` (vendored courier JSON) silently absent from git.

## Root cause
- `pipeline.health._raw_env` merges process env only for keys matching `_ENV_PREFIXES`; a new feature's prefix must be added there AND to `tests/conftest.py` CONFIG_PREFIXES.
- `health.known_sources()` is asserted verbatim across test_health, test_api_collectors, test_api_startup, test_legacy_alias, test_management_e2e, test_runner_only, test_unseeded_env.
- `next_check_at = now + interval` with `now` taken after a variable-length scan drifts past the next cron fire.
- ETAs are calendar dates stored as naive midnight; converting midnight UTC to a local zone moves it to the previous day.
- UPS Track activity `date`/`time` are local to the scan site; `gmtDate`/`gmtTime`/`gmtOffset` carry UTC.
- `.gitignore` rule `data/` matches any `data` directory, not just the root one.

## Fix
- `pipeline/health.py` `_ENV_PREFIXES` tuple (regression test in tests/test_packages_settings.py).
- Appended `"packages"` to every pinned source list (commits a3dafb9, b472dcc).
- `pipeline/packages/poll.py` `DUE_TOLERANCE` (10 min): due when `next_check_at <= now + DUE_TOLERANCE`; interval read from `packages_settings()`.
- `api/routers/packages.py` summary compares `p.eta.date()` to local today; only real timestamps (`delivered_at`) are tz-converted.
- `carriers/ups.py` `_activity_when`: prefer gmt fields, else subtract offset; match the DEL activity on its LOCAL date, store its UTC time.
- `.gitignore` `!pipeline/packages/data/`.
- `inventory.add_manual` returns `(package, created)`; router catches IntegrityError → 409 (replaced count-all-rows-before/after).

## Verify command
`venv/bin/python -m pytest tests/test_packages_settings.py tests/test_packages_poll.py tests/test_api_packages.py tests/test_packages_carrier_ups.py -q`

## Pitfalls
- New `<FEATURE>_` env prefix: update `health._ENV_PREFIXES` and `tests/conftest.py` together.
- New always-on source: `grep -rn '"jobs"' tests/` and extend every pinned list; it is not a regression in those suites.
- Never set a poll's next due time to exactly the cron period; add tolerance.
- Never tz-convert a date-only value; compare `.date()`.
- Carrier event times: confirm UTC vs local per API before storing naive-UTC.
- Never correlate shipments on Gmail threadId (Gmail merges unrelated same-subject mail); key on (carrier, number) else (merchant_domain, order_id).
- Sender rules beat checksums: a Walmart `555…` last-mile number passes the USPS 20 check.
- Frontend list/detail loads need a request-sequence guard after every await.
- Cloud container: init `data/digest.db` (`db.init_db()`) and align the Playwright browser revision, or ~27 baseline tests fail for environment reasons.

## Related files
- pipeline/health.py: env merge and known_sources
- pipeline/packages/poll.py: carrier polling cadence
- pipeline/packages/carriers/ups.py: UPS time normalisation
- api/routers/packages.py: summary buckets, manual add
- pipeline/packages/numbers.py: vendored jkeen rules, S10 country codes
- web/utils/status.ts: shared `package` StatusKind
