# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]
### Changed
- One `request()`/`ApiError` implementation for every web composable (`web/utils/api/request.ts`) instead of four copies
- One retry core for the ATS adapters (`pipeline.jobs.ats.fetch_json/fetch_text/post_json`) instead of four forks
- Shared API helpers: `api.errors.coded`, `api.serialize.scrub`, `api.schemas.ImportOut`; scheduler jobs built from one `_cron_trigger`/`_upsert_job` pair
- `iso_z` lives in `pipeline.clock` (api.serialize re-exports it); registrar timeout and `domain_tld` shared; health checks for local sources share one implementation
### Fixed
- `PUT /api/config` rejects an invalid `schedule_cron` with 422 instead of persisting it; a bad stored value no longer stops the scheduler, and the app, at startup
- Tests run against a per-test database instead of the repository's `data/digest.db`: 26 tests failed on a clean checkout and the suite wrote `data/` files
- `next_run_time()` returns None for a job pending on a not-yet-started scheduler instead of raising
### Documentation
- README: phases and sources reflect the Domains/Jobs/Packages consoles and the unwired triage stage; `MSTODO_ORG_ALIAS` (never read) and the unused `ChannelMessage.Read.All` scope removed; pages list, test prerequisites, Namecheap sandbox host, `ALLOWED_HOSTS` wording corrected

## [1.3.0] - 2026-10-12
### Added
- Vendored jkeen tracking_number_data courier rules (MIT) for tracking number detection ([3a4da66](https://github.com/lasswellt/signalslate/commit/3a4da66))
- Package, PackageEvent and PackageEmail tables (email metadata only, no bodies stored) ([3bfb5aa](https://github.com/lasswellt/signalslate/commit/3bfb5aa))
- Packages settings: refresh cron, scan window, stale threshold, UPS/USPS developer keys ([14459c7](https://github.com/lasswellt/signalslate/commit/14459c7))
- Tracking number matcher with checksum validators and carrier-context rules for weak formats ([2de9809](https://github.com/lasswellt/signalslate/commit/2de9809))
- Link and text harvester that decodes awstrack and Mandrill redirect links locally ([c5efbd0](https://github.com/lasswellt/signalslate/commit/c5efbd0))
- Carrier tracking deep links built from vendored URL templates ([4c0d645](https://github.com/lasswellt/signalslate/commit/4c0d645))
- Tiered shipment extraction from shipping mail (labels, links, sender rules, order-only) ([fc630de](https://github.com/lasswellt/signalslate/commit/fc630de))
- Per-sender status classifier with ETA parsing and noise rejection ([6994dbf](https://github.com/lasswellt/signalslate/commit/6994dbf))
- Package inventory with correlation by carrier and number or merchant order, monotonic status, stale and archive ([c256917](https://github.com/lasswellt/signalslate/commit/c256917))
- Read-only Gmail scan with a cursor and a 60-day first run ([c3a1f2c](https://github.com/lasswellt/signalslate/commit/c3a1f2c))
- Carrier adapter base with OAuth token cache and rate limiter ([ee98ff1](https://github.com/lasswellt/signalslate/commit/ee98ff1))
- UPS Track API adapter ([850627c](https://github.com/lasswellt/signalslate/commit/850627c))
- USPS Tracking v3 adapter ([9b1aa1e](https://github.com/lasswellt/signalslate/commit/9b1aa1e))
- UPS/USPS polling every 2 hours, stopping 48 hours after delivery ([dec4adb](https://github.com/lasswellt/signalslate/commit/dec4adb))
- Scheduled packages_refresh job (scan, then poll) ([a466e38](https://github.com/lasswellt/signalslate/commit/a466e38))
- 'packages' digest source for delivered, out-for-delivery and arriving packages ([0f54e57](https://github.com/lasswellt/signalslate/commit/0f54e57))
- /api/packages routes: list, detail, add, archive, delete, scan, refresh, and a Home Assistant summary ([6770bcb](https://github.com/lasswellt/signalslate/commit/6770bcb))
- Typed usePackagesApi composable ([90a39b9](https://github.com/lasswellt/signalslate/commit/90a39b9))
- Package detail dialog with event timeline, source emails, tracking link and archive ([5caf6ff](https://github.com/lasswellt/signalslate/commit/5caf6ff))
- Add-package dialog with server-side carrier detection ([5ffafe4](https://github.com/lasswellt/signalslate/commit/5ffafe4))
- /packages page with status chips, filters, summary and Scan/Refresh actions, plus a Tools nav entry ([53c8cd6](https://github.com/lasswellt/signalslate/commit/53c8cd6))
- End-to-end backend test from fixture mail to digest and summary, asserting no email body is stored ([8cda439](https://github.com/lasswellt/signalslate/commit/8cda439))

### Fixed
- DOMAINS_, JOBS_, PACKAGES_, UPS_ and USPS_ settings are now read from the process environment, not only .env ([0a6bb20](https://github.com/lasswellt/signalslate/commit/0a6bb20))
- Packages are polled on every 2-hour run (due tolerance); a failing carrier stops after 3 errors and records last_error ([554c659](https://github.com/lasswellt/signalslate/commit/554c659))
- "Arriving today" no longer counts a day early west of UTC; duplicate adds return 409; input bounds; no N+1 in the list ([aa881f9](https://github.com/lasswellt/signalslate/commit/aa881f9))
- UPS event times are stored in UTC ([e3d4edf](https://github.com/lasswellt/signalslate/commit/e3d4edf))
- UPS delivered_at matches the delivery's local date ([4db0f5e](https://github.com/lasswellt/signalslate/commit/4db0f5e))
- Packages page and detail dialog ignore out-of-order responses ([03935ee](https://github.com/lasswellt/signalslate/commit/03935ee))
- S10 (UPU) numbers with unknown country codes are rejected ([b1a64f6](https://github.com/lasswellt/signalslate/commit/b1a64f6))

### Documentation
- README Packages section (carrier key signup, privacy, limits, Home Assistant sensor) and .env.example keys ([a97a4b5](https://github.com/lasswellt/signalslate/commit/a97a4b5))
- Package tracking research, validated against a real mailbox sample ([8b446ce](https://github.com/lasswellt/signalslate/commit/8b446ce))
