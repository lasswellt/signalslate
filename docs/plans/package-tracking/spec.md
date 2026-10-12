---
status: active
priority: P1
created: 2026-10-10
ship: manual
---
# Package tracking: Gmail purchase scan, email-first status, UPS/USPS polling

## Goal
Find every package the owner orders by scanning their connected Gmail accounts for shipping mail, and show each package's live status on one `/packages` page without paid aggregators or scraping. Status comes first from the email the owner already receives (Walmart, Home Depot, FedEx Delivery Manager, DHL On Demand, SEFL freight, Shopify-template stores, other merchants); UPS and USPS packages are additionally polled through the free carrier APIs once the owner adds developer keys. Delivered and arriving packages reach the morning digest, and a summary endpoint feeds Home Assistant. Research and real-mailbox validation: `docs/_research/2026-10-10_package-tracking.md`.

## Outcomes
- A scheduled scan over each Gmail account (first run 60 days back, then incremental) creates one package per shipment with carrier, tracking number or order id, merchant and source emails; extraction meets the 20 cases in `tests/fixtures/packages/extraction_cases.json`. → T-001, T-002, T-003, T-004, T-005, T-007, T-009, T-010, T-015, T-022
- Package status advances from later emails (Walmart, Home Depot, FedEx, DHL, SEFL, Shopify template, merchants) and never regresses; packages without a terminal email go `stale` after 30 days. → T-008, T-009, T-010, T-022
- With `UPS_CLIENT_ID/SECRET` or `USPS_CLIENT_ID/SECRET` set, UPS and USPS packages are polled every 2 hours within carrier rate limits, stop 48 hours after delivery, and stay dormant without keys. → T-011, T-012, T-013, T-014, T-015, T-022
- The owner opens `/packages` from the Tools nav, sees a table with status chips, carrier deep links and last event, opens a detail timeline, adds a number by hand, archives, and triggers scan or refresh. → T-006, T-017, T-018, T-019, T-020, T-021
- The digest run includes a `packages` source with delivered and out-for-delivery items in the window, and `GET /api/packages/summary` returns counts for Home Assistant. → T-016, T-017, T-022

## Out of scope
- Scraping carrier or Google pages; paid or freemium aggregators (17TRACK, AfterShip, Ship24); Karrio.
- FedEx and DHL API adapters (their notification mail covers this mailbox); OnTrac, Amazon TBA and Walmart `555…` carrier lookups (no public API).
- LLM extraction; JSON-LD parsing (0 of 3 bodies carried it).
- Following click-tracking redirects (privacy leak); links are only decoded locally.
- Rendering packages in the PDF (digest phases 3–4 not built); carrier key entry in the UI.
- Storing email bodies.

## Assumptions
- Interview answers: include UPS + USPS adapters; add a digest source; add the Home Assistant summary endpoint; first scan reaches 60 days.
- Carrier keys are read from `.env` through `health.env()` (like `ANTHROPIC_API_KEY`), not a Connection kind, to keep surface small; a UI form is a follow-up.
- `jkeen/tracking_number_data` is vendored (MIT) rather than the stale `tracking-numbers` PyPI port (last release 2022-11-04).
- UPS and USPS response shapes come from vendor docs, not live calls; adapters keep parsing behind one function with typed errors, as the domains registrars did. USPS 60/hour is a third-party figure and lives as a named constant.
- Outbound HTTP uses `requests` (codebase convention); tests stub it at the module import site.
- Gmail access reuses the existing `gmail.readonly` OAuth (`pipeline/health.py:76`); no new scope.

## Verification
- `bash /root/.claude/skills/blitz-cc/scripts/tasks.sh verify package-tracking <id>` per task; `/blitz:check --scope plan package-tracking` before ship.
- Manual: after deploy, run `POST /api/packages/scan` against the real mailbox and compare the page against the research Validation tables (Walmart order-level packages, FedEx 12-digit numbers, Home Depot `1R00…`).
