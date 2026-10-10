---
topic: package-tracking
type: Feature Investigation
date: 2026-10-10
status: final
critic: PASS (2026-10-10; 17 LIVE, 1 UNKNOWN UPS PDF; corrections applied)
agents: [codebase-analyst, library-docs, web-researcher]
agents_succeeded: 3/3  # library-docs + web-researcher returned PARTIAL (gaps listed in §Open Questions)
citations:
  - url: "https://github.com/jkeen/tracking_number_data"
    title: "tracking_number_data (carrier regex + checksum JSON)"
    pub_date: "living"
    claimed_span: "MIT; 18 couriers incl. Amazon, DHL, FedEx, LaserShip, OnTrac, UPS, USPS; regex with SerialNumber/CheckDigit groups; mod10, mod7, s10, luhn validators; test_numbers"
  - url: "https://pypi.org/pypi/tracking-numbers/json"
    title: "tracking-numbers (Python port) PyPI metadata"
    pub_date: "2022-11-04"
    claimed_span: "version 0.1.8, License MIT, last upload 2022-11-04T19:28:36"
  - url: "https://developers.google.com/workspace/gmail/markup/reference/parcel-delivery"
    title: "Gmail markup: ParcelDelivery"
    pub_date: "living"
    claimed_span: "trackingNumber, carrier, trackingUrl, expectedArrivalFrom/Until; JSON-LD and microdata"
  - url: "https://developers.google.com/workspace/gmail/api/reference/quota"
    title: "Gmail API usage limits"
    pub_date: "living"
    claimed_span: "messages.get 20 units; 6,000 units per user per minute"
  - url: "https://www.usps.com/business/web-tools-apis"
    title: "USPS Web Tools APIs (retirement)"
    pub_date: "2026-01"
    claimed_span: "Web Tools retired January 25, 2026"
  - url: "https://developers.usps.com/trackingv3"
    title: "USPS Tracking 3.0 (page labelled Legacy)"
    pub_date: "living"
    claimed_span: "Tracking 3.0 product page exists; OAuth 2.0 client-credentials detail not shown on this page (UNVERIFIED here)"
  - url: "https://developers.usps.com/faq"
    title: "USPS developer FAQ"
    pub_date: "living"
    claimed_span: "429 over quota; higher quota by service request (60 calls/hour default comes from third-party client READMEs, not this page)"
  - url: "https://developer.fedex.com/api/en-us/catalog/track/v1/docs.html"
    title: "FedEx Track API"
    pub_date: "living"
    claimed_span: "up to 30 tracking numbers per request; account number required for SPOD signature; reference tracking possible with postal code, country, ship date range"
  - url: "https://developer.dhl.com/tracking"
    title: "DHL Shipment Tracking - Unified"
    pub_date: "living"
    claimed_span: "new apps 250 calls/day, max 1 call per 5 seconds; initial tier described as for development"
  - url: "https://filexfer.ups.com/assets/resources/webcontent/en_US/ups_api_access_agreement.pdf"
    title: "UPS API Access Agreement"
    pub_date: "living"
    claimed_span: "Developer definition includes individuals (UNVERIFIED: PDF text not extractable by critic)"
  - url: "https://github.com/karrioapi/karrio"
    title: "Karrio"
    pub_date: "living"
    claimed_span: "LGPL-3.0 (server, plugins, core; /ee separate license); Docker self-host; 4GB recommended for hobby deploy"
  - url: "https://github.com/moralmunky/Home-Assistant-Mail-And-Packages"
    title: "HA Mail and Packages"
    pub_date: "living"
    claimed_span: "connects to the email account you supply; matches subject lines of current day's email; counts in-transit/delivered"
  - url: "https://parcelsapp.com/en/shops/amazon"
    title: "ParcelsApp: Amazon tracking"
    pub_date: "living"
    claimed_span: "Amazon Logistics TBA numbers not trackable by third parties"
  - url: "https://github.com/WolffRuoff/package_tracker/pull/30"
    title: "package_tracker: USPS page migration fix"
    pub_date: "2026-07"
    claimed_span: "USPS tracking page moved; scraper selectors broke"
  - url: "https://scrapfly.io/blog/how-to-bypass-akamai-anti-scraping"
    title: "Akamai anti-scraping"
    pub_date: "2026-10-05"
    claimed_span: "blocks can return HTTP 200 with challenge page"
  - url: "https://community.home-assistant.io/t/17track-alternatives/903146"
    title: "HA community: 17track alternatives"
    pub_date: "2025"
    claimed_span: "17track integration breaking changes and deprecated sensors; ParcelsApp ~10 parcels/month"
  - url: "https://support.google.com/cloud/answer/15549945"
    title: "Google Cloud: OAuth app publishing status"
    pub_date: "living"
    claimed_span: "Testing-mode authorizations expire after seven days"
---

# Package tracking from Gmail

## Summary

Build a `pipeline/packages/` feature that searches each connected Gmail account for shipping mail, extracts tracking numbers deterministically, and tracks status from three free sources in priority order: **(1) the shipping emails themselves**, **(2) official carrier APIs where the user has supplied a free developer key** (USPS, UPS, FedEx, DHL), and **(3) a carrier deep link** for anything else. No scraping and no paid aggregator. Phase 1 needs zero new credentials and already covers Amazon, the carrier that no API can track.

## Research Questions

| # | Question | Answer (short) |
|---|---|---|
| Q1 | Can the existing Gmail integration search older purchase mail? | Scope yes, collector no. `gmail.readonly` covers any `q`; the collector hard-codes an epoch window capped at 7 days. Needs a dedicated scanner. |
| Q2 | How to detect tracking numbers reliably? | Tiered: JSON-LD/microdata, then carrier URLs in `href`s, then regex + check digit + keyword proximity. LLM optional, enrichment only. |
| Q3 | What free status sources exist per carrier? | USPS v3, UPS Track, FedEx Track, DHL Unified are free with a developer key. Amazon TBA, LaserShip, OnTrac have no usable public API: email-derived status only. |
| Q4 | Is scraping carrier sites viable? | No. Akamai bot protection and page migrations (USPS, July 2026) make it a maintenance sink. |
| Q5 | Are free aggregators (17TRACK, AfterShip, Ship24) an option? | Not as a dependency. Free tiers are 10-100 parcels/month and changing; AfterShip API needs a paid plan. |
| Q6 | How does it fit the codebase? | Mirrors `domains`/`jobs`: models in `pipeline/db.py`, router, scheduler job, composable, page under Tools nav. |

## Findings

### Q1. Gmail access (codebase)

- Scope is already sufficient: `GMAIL_SCOPE = gmail.readonly` (`pipeline/health.py:76`). `category:purchases`, `subject:(...)`, `newer_than:` all work under it.
- The digest collector's query is only `after:<epoch> before:<epoch>` (`pipeline/collectors/gmail.py:153-155`), window from `runner.collection_window` with 24h default and 7-day `MAX_BACKFILL` (`pipeline/collectors/__init__.py:19-25`). Collected `CollectedItem` rows therefore never hold mail older than 7 days. A purchase scanner must call Gmail directly.
- Reusable pieces in `pipeline/collectors/gmail.py`: `_call` with 429/5xx/rateLimit backoff (`:116-142`), `fetch_messages` (`format=full`, `:452`), `flatten_message` (`:393-430`); tokens via `health.gmail_token_response(label)` (`pipeline/health.py:614`), accounts via `health.gmail_accounts()` (`:301`).
- **Gap that matters:** `_TextExtractor` keeps only text nodes (`handle_data`, `gmail.py:253`) and ignores `href`. "Track package" buttons, the most reliable tracking source, are discarded. The scanner needs its own HTML pass that keeps `a[href]` and `<script type="application/ld+json">`.
- Quota is a non-issue at single-user scale: `messages.list` + `messages.get` per hit, already paced 0.3s in the collector (`gmail.py:53`). Gmail quota page lists per-method units and a per-user per-minute cap (`messages.get` = 20 units, 6,000 units/user/minute; irrelevant at hundreds of messages).
- Token lifetime risk is already handled by the Gmail research (consent screen published to Production, `docs/_research/2026-09-20_gmail-collector-and-agents.md:23`). No new OAuth work.

### Q2. Tracking number extraction

Precision ordering, highest first:

| Tier | Source | Precision | Notes |
|---|---|---|---|
| 1 | schema.org `ParcelDelivery` / `Order.orderDelivery` (JSON-LD or microdata) | Exact | Gives carrier, number, ETA, merchant. Prevalence unmeasured; Gmail requires sender registration, so treat as a bonus. |
| 2 | Carrier tracking URLs in `href` (`ups.com/track?tracknum=`, `tools.usps.com/go/TrackConfirmAction?tLabels=`, `fedex.com/fedextrack/?trknbr=`, `dhl.com/...?tracking-id=`, Amazon `progress-tracker` with `orderId`/`shipmentId`) | High | Carrier comes from the host. Click-wrapper redirects (`click.*`, `links.*`) hide these; resolving them leaks a "viewed" signal to the merchant. Default: do not follow redirects; decode URL-encoded targets when present in query params. |
| 3 | Regex + check digit from `jkeen/tracking_number_data` | Medium | MIT, 18 couriers, regex with named `SerialNumber`/`CheckDigit` groups and mod10/mod7/S10/luhn validators, ships `test_numbers`. Only `1Z…` (UPS) and S10 (`AA123456789US`) are distinctive; pure-digit FedEx/USPS formats collide with order and phone numbers. Require check-digit pass **and** a carrier/"tracking" keyword within ~80 chars. |
| 4 | LLM (Haiku via existing `anthropic` dep, `pipeline/health.py:211`) | Variable | Merchant name, order id, item summary, ETA. Off by default for this feature: it sends email bodies to a third party, which conflicts with the "open and free" brief. Pattern exists in `pipeline/jobs/inbox_seed.py:83-154`. |

- Vendor the JSON data (MIT, copy `couriers/*.json` + LICENSE into `pipeline/packages/data/`) and write a ~100-line matcher. The Python port `tracking-numbers` is MIT but last released 2022-11-04 (0.1.8) and embeds a stale data snapshot.
- No measured false-positive rates exist in public sources. Build a fixture corpus from real mail (redacted) and track precision in tests.

### Q3. Free status sources per carrier

| Carrier | Free source | Auth | Limits | Individual OK? | Verdict |
|---|---|---|---|---|---|
| USPS | Tracking v3 at `apis.usps.com` (Web Tools XML retired 2026-01-25) | OAuth2 client credentials (per third-party clients) | 429 over quota, increase by service request; 60 calls/hour default per third-party clients only | Yes, via Business Customer Gateway account; April 2026 "API Access Control" change unverified | Use |
| UPS | Track API (developer.ups.com) | OAuth2 client credentials | Not published | Access agreement covers individuals; asks company name + phone; account-number need for Track unverified | Use |
| FedEx | Track API v1 | OAuth2 client credentials | Not published; 30 numbers/request | Project creation reportedly requires linking a 9-digit FedEx account; Track by number needs no account number; SPOD signature does | Use if user has/creates a free fedex.com shipping account |
| DHL | Shipment Tracking - Unified | API key header | 250 calls/day, max 1 call per 5s; starter tier labelled for development, upgrades restricted to DHL customers | Yes | Use (250/day suffices for one user) |
| Amazon Logistics (TBA…) | None. Not trackable by third parties; requires Amazon login | n/a | n/a | n/a | Email-derived only |
| LaserShip/OnTrac, GOFO, regional | None free | n/a | n/a | n/a | Email-derived + deep link |
| International (S10 postal) | Origin post's site; 17TRACK free tier | varies | 17TRACK ~50-100/month | n/a | Deep link; aggregator not adopted |

**Email-derived status** is the universal fallback and the only Amazon path. Merchants and Amazon send "shipped", "out for delivery", "delivered" mail that carries the same tracking number, order id, or Gmail `threadId`. Each scan re-reads new matching mail and advances package state monotonically. HA Mail and Packages proves the approach works (subject-line matching), and also shows its limit: wording drifts and small merchants send nothing after "shipped".

**Polling budget** (single user, ~10-30 active packages): poll each active package every 2h, at most 12/day. USPS 60/hr and DHL 250/day (1 call per 5s) both hold; batch DHL numbers per call where the API allows. Stop polling 48h after `delivered`; mark `stale` after 30 days without an event.

### Q4. Scraping

Rejected. UPS sits behind Akamai, which returns HTTP 200 challenge pages so failures look like successes. USPS moved its tracking page in July 2026 and broke known scrapers. Old HA threads report USPS flagging polling IPs. A LAN-only box with a residential IP would risk getting the owner's home IP flagged on carrier sites they use manually.

### Q5. Aggregators and Karrio

| Option | Free tier | Why not |
|---|---|---|
| 17TRACK API | ~50-100 numbers/month, figure conflicting | Volatile; HA's 17track integration users report breaking changes and deprecated sensors; third-party data processor |
| AfterShip | API only on paid Pro (~$119/mo) | Paid |
| Ship24, TrackingMore, ParcelsApp | 10-100/month trial-like | Volatile, ToS for personal automation unclear |
| Karrio (LGPL-3.0, self-host) | Free | 4 GB recommended even for a hobby deploy; a full shipping platform wrapping the same carrier APIs (carrier credentials still expected, per general Karrio design, not stated on README). Overkill for one user; worth revisiting only if label purchasing ever enters scope. |

### Q6. Codebase integration

Pattern from `domains` and `jobs`, verified:

- Models: SQLModel tables in `pipeline/db.py`; `init_db` auto-creates new tables and adds nullable columns (`pipeline/db.py:324-357`).
- Scheduler: job id constants at `api/scheduler.py:58-60`, `add_job`/`reschedule_job` pattern at `:114-137`. Add `PACKAGES_JOB_ID = "packages_refresh"`.
- Router: `api/routers/domains.py` shape (`_coded` errors, pydantic Out models), registered in `api/main.py` with `/api` prefix.
- Nav: Tools group in `web/layouts/default.vue:36-40`; add `{ label: 'Packages', to: '/packages', icon: 'local_shipping', slug: 'packages' }` and the `nav-packages` testid in `web/tests/smoke.test.ts`.
- Design: `web/DESIGN.md` requires status chips as icon + label (never colour alone), never the teal accent on status, dense 25-row tables, skeleton/empty/error states, `PageHeader`, `DialogShell`.
- Secrets: carrier keys via env through `health.env()` (like `ANTHROPIC_API_KEY`) for Phase 2; promote to an encrypted `Connection` kind (`pipeline/connections.py` `KINDS`) only if UI entry is wanted.
- HTTP: `httpx==0.28.1` and `requests==2.34.2` both present; no shared retry util, copy the `gmail.py:80-142` backoff.

## Dissent / Contradictory Evidence

- **Email-only vs API polling.** The contrarian pass argues email-derived status is uneven (late or missing "delivered" mail from small merchants). That is why carrier APIs stay in scope for the four big carriers. Conversely, carrier APIs add per-carrier signup friction (FedEx account link, USPS BCG, UPS company fields) for a personal tool. Resolution: make APIs opt-in per carrier, ship email-derived first, measure how often packages stall in `in_transit`.
- **JSON-LD value.** library-docs called ParcelDelivery markup anecdotally common among large merchants; web-researcher found Gmail markup requires sender registration and no adoption stats. Both agree: parse it when present, never depend on it.
- **USPS quota and access rules.** USPS's FAQ confirms 429 and quota requests but not the 60/hr figure, which comes from third-party packages; the April 2026 Access Control change could alter individual eligibility. Verify at signup before building the adapter.
- **LLM extraction.** Codebase already ships Anthropic Haiku and an extraction pattern, so it is cheap to add. It contradicts "open and free" (third-party processing of order mail). Kept optional, off by default.

## Recommendation

### Decision

Ship a `packages` feature in two phases:

- **Phase 1 (no new credentials):** Gmail purchase scanner + deterministic extractor (JSON-LD, href URLs, vendored jkeen regex with check digits) + email-derived status state machine + `/packages` page with carrier deep links and manual add/archive.
- **Phase 2 (opt-in keys):** carrier status adapters for USPS v3, UPS Track, FedEx Track, DHL Unified behind a common `CarrierAdapter` protocol, polled by an APScheduler job with per-carrier rate limits. Adapters stay dormant when their env keys are absent.

Explicitly out: scraping, paid or freemium aggregators, Karrio, LLM extraction by default.

### Rationale

- Amazon is likely the largest share of a personal package volume and is only trackable through email, so email-derived status is mandatory regardless; building it first delivers value with zero signup.
- Official APIs are free, stable, and documented; scraping is neither stable nor polite; aggregators are neither free at volume nor durable.
- Check-digit validation plus `href` harvesting addresses the main extraction risk (numeric false positives) without an LLM.
- Every piece maps onto an existing pattern (`domains`/`jobs`, Gmail helpers, scheduler, Vault), so new surface area is small.

### Implementation Sketch

```
pipeline/packages/
  __init__.py        PackagesSettings: PACKAGES_REFRESH_CRON ("23 */2 * * *"), PACKAGES_SCAN_DAYS (60),
                     PACKAGES_SCAN_QUERY, PACKAGES_LLM_ENRICH (false)
  data/              vendored jkeen couriers/*.json + LICENSE (MIT)
  numbers.py         load couriers, regex + checksum validators, keyword-proximity gate
  html.py            href + JSON-LD + microdata harvester (separate from gmail._TextExtractor)
  extract.py         tiered extraction -> list[Candidate(carrier, number, source_tier, order_id, merchant, eta)]
  email_status.py    keyword rules -> ordered status; monotonic transitions
  scan.py            per gmail account: q + after:<cursor epoch>; reuse gmail._call/fetch_messages/flatten_message
  carriers/
    base.py          CarrierAdapter Protocol: code, configured(), track(numbers) -> list[TrackResult]; RateLimiter
    usps.py ups.py fedex.py dhl.py     (Phase 2)
    links.py         deep-link URL per carrier (Phase 1)
  inventory.py       scan_all(), refresh_all(): due packages, batching, stop rules
api/routers/packages.py   GET /packages, GET /packages/{id}, POST /packages, PATCH /packages/{id}, DELETE,
                          POST /packages/scan, POST /packages/refresh
api/scheduler.py          PACKAGES_JOB_ID + trigger/reschedule/next_run (mirror DOMAINS_JOB_ID)
api/main.py               include_router
pipeline/db.py            Package, PackageEvent, PackageEmail
web/composables/usePackagesApi.ts
web/pages/packages.vue    PageHeader + PackageTable (status chip, carrier, merchant, ETA, last event, deep link)
web/components/PackageTable.vue, PackageDetailDialog.vue (event timeline + source emails), PackageAddDialog.vue
web/layouts/default.vue   Tools nav item
```

Default Gmail query (per account, plus `after:<last_scan_epoch>`; first run `newer_than:60d`):

```
(category:purchases OR subject:(shipped OR shipment OR "on its way" OR "out for delivery"
  OR delivered OR tracking OR "has shipped")) -in:spam -in:trash
```

Data model:

```
Package(id, carrier, tracking_number, UNIQUE(carrier, tracking_number), merchant, order_id,
        status: pending|in_transit|out_for_delivery|delivered|exception|returned|stale|unknown,
        status_source: email|carrier|manual, eta, delivered_at, last_event_at, last_checked_at,
        next_check_at, archived, created_at)
PackageEvent(id, package_id FK, occurred_at, status, description, location, source, dedupe_hash UNIQUE)
PackageEmail(id, package_id FK, account_label, message_id, thread_id, subject, sender, received_at)
```

Amazon handling: key on `order_id` + `threadId` when no carrier number exists (carrier `amazon`, synthetic number `order:<id>`), status from Amazon's own mail only.

Status chip mapping (DESIGN.md: icon + label): pending `schedule`, in_transit `local_shipping`, out_for_delivery `near_me`, delivered `check_circle`, exception `error`, returned `undo`, stale `help`.

Optional follow-up: `GET /api/packages/summary` for a Home Assistant REST sensor (count out for delivery today), and a digest section for packages delivered or arriving today via `pipeline/collectors/__init__.py` dispatch.

### Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Numeric false positives (order/phone numbers) | High without checks | Check digit + keyword proximity + per-sender suppress list; fixture corpus with precision assertions |
| Amazon status lag or missing "delivered" mail | Medium | Show `last_event_at` age; mark `stale`; deep link to Amazon order page |
| Carrier signup friction (FedEx account link, USPS BCG) | Medium | Adapters opt-in; Phase 1 useful without them |
| USPS/UPS terms or quota change | Medium | Adapter isolation, 429 backoff, `last_error` surfaced per carrier on the page |
| Click-wrapped tracking links | Medium | Decode embedded targets; never auto-follow; fall back to regex |
| Gmail token expiry | Low (already mitigated) | Existing Production consent screen; reuse health checks |

## Open Questions

1. USPS April 2026 "API Access Control": does an individual BCG account still get Tracking v3? Verify at signup.
2. UPS: does Track require an account number on the app? Docs page returned 503 during research.
3. FedEx: confirm a free personal fedex.com account satisfies the project's account-link requirement.
4. Real precision of the extractor on this user's mail: measure from a redacted 200-message sample before tuning.
5. Should packages appear in the daily reMarkable digest? Product call, not a research gap.

## Sources

See frontmatter `citations`. Codebase claims cite `path:line`, verified on branch `claude/great-johnson-7qeay0` at `c966b01`.
