---
topic: package-tracking
type: Feature Investigation
date: 2026-10-10
status: final
validated: 2026-10-10 against user Gmail (read-only, ~40 packages)
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

Build a `pipeline/packages/` feature that searches each connected Gmail account for shipping mail, extracts tracking numbers deterministically, and tracks status from three free sources in priority order: **(1) the shipping emails themselves**, **(2) official carrier APIs where the user has supplied a free developer key** (USPS, UPS, FedEx, DHL), and **(3) a carrier deep link** for anything else. No scraping and no paid aggregator. Validated against the user's Gmail on 2026-10-10: about 90% of packages reach delivered from Walmart, FedEx and DHL mail alone, so the carrier API set shrinks to an optional UPS adapter (see Validation and the revised Decision). Phase 1 needs zero new credentials. Scope: the user does not shop Amazon; the primary merchant is **Walmart (Walmart+)**, which splits into carrier-shipped orders (tracking number in mail) and store-delivered orders (no tracking number, email-only status).

## Research Questions

| # | Question | Answer (short) |
|---|---|---|
| Q1 | Can the existing Gmail integration search older purchase mail? | Scope yes, collector no. `gmail.readonly` covers any `q`; the collector hard-codes an epoch window capped at 7 days. Needs a dedicated scanner. |
| Q2 | How to detect tracking numbers reliably? | Tiered: JSON-LD/microdata, then carrier URLs in `href`s, then regex + check digit + keyword proximity. LLM optional, enrichment only. |
| Q3 | What free status sources exist per carrier? | USPS v3, UPS Track, FedEx Track, DHL Unified are free with a developer key. OnTrac (merged with LaserShip; a Walmart carrier) and Walmart store delivery have no usable public API: email-derived status only. Amazon out of scope. |
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
| 2 | Carrier tracking URLs in `href` (`ups.com/track?tracknum=`, `tools.usps.com/go/TrackConfirmAction?tLabels=`, `fedex.com/fedextrack/?trknbr=`, `dhl.com/...?tracking-id=`, Walmart `walmart.com/orders/<id>` links for order id) | High | Carrier comes from the host. Click-wrapper redirects (`click.*`, `links.*`) hide these; resolving them leaks a "viewed" signal to the merchant. Default: do not follow redirects; decode URL-encoded targets when present in query params. |
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
| Walmart store delivery (Walmart+ same-day/express via Spark drivers) | None. No carrier tracking number; status lives in the Walmart app and Walmart mail | n/a | n/a | n/a | Email-derived only, keyed by order id |
| LaserShip/OnTrac, GOFO, regional | None free | n/a | n/a | n/a | Email-derived + deep link |
| International (S10 postal) | Origin post's site; 17TRACK free tier | varies | 17TRACK ~50-100/month | n/a | Deep link; aggregator not adopted |

**Email-derived status** is the universal fallback and the only path for Walmart store delivery and OnTrac. Merchants send "shipped", "out for delivery", "delivered" mail that carries the same tracking number, order id, or Gmail `threadId`. Each scan re-reads new matching mail and advances package state monotonically. HA Mail and Packages proves the approach works (subject-line matching), and also shows its limit: wording drifts and small merchants send nothing after "shipped".

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

### Q4b. Google search tracking box (follow-up, 2026-10-10)

Rejected. Google's tracking box is fed by carriers' own APIs that Google calls live; the partner program no longer accepts new carriers (https://developers.google.com/search/docs/appearance/package-tracking, updated 2026-07-14). Automated `google.com/search` fetches are disallowed by `robots.txt` (`Disallow: /search`). The Custom Search JSON API is closed to new customers and discontinued 2027-01-01. Gmail's own inbox package tracking has no API. Same carrier data, worse access path.

## Validation (Gmail sample, 2026-10-10)

Method: read-only Gmail connector against the user's mailbox, `newer_than:180d`. Searched `from:walmart.com`, the shipping-subject query, and carrier senders; read 16 message bodies in full (plain text + 3 raw HTML), and the rest by subject and snippet. Check digits were tested with a scratch matcher over the vendored `jkeen/tracking_number_data` rules. No mail content is committed; `tests/fixtures/packages/extraction_cases.json` holds synthetic look-alikes.

### What the mailbox actually contains (last ~60 days, by package)

| Source | Packages seen | Tracking number in mail? | Status mail through delivery? | Free status path |
|---|---|---|---|---|
| Walmart store / InHome delivery (Spark drivers) | ~12 orders | No | Yes: confirmation, out for delivery, Delivered, for every order sampled | Walmart mail, keyed by order number |
| Walmart "Fulfilled by Walmart" ship-to-home | 9 | Yes: 20-digit `555…` "Track your package:" | Yes: Shipped, Out for delivery, Arrived, for all 9 | Walmart mail. The `555…` number is Walmart's own last-mile; the out-for-delivery mail says "Delivery from store" and names the driver. No carrier API knows it. |
| Walmart via FedEx | 1 | Yes (FedEx 12) | Yes, via FedEx mail | FedEx Delivery Manager mail |
| Other merchants via FedEx (B&H, Whisker, Home Depot, Lamps Plus, others) | 13 | Yes | Yes: FedEx sends on the way, delivery tomorrow, out for delivery, delivered, every time | FedEx Delivery Manager mail (subject carries the number; body has ETA window, shipper, merchant order no. as "Reference") |
| DHL Express | 2 | Yes (waybill) | Yes: DHL On Demand Delivery mail | DHL mail |
| UPS (HeyDude, via Narvar link) | 1 | Yes (`1Z`, in text and in an unwrapped Narvar URL) | **No**: no follow-up 5 days after "shipped"; no UPS My Choice mail in the mailbox | UPS Track API (Phase 2) or deep link |
| UPS (Home Depot "Delivery by: UPS") | 1 | Yes, but a non-`1Z` 28-char id | Yes, Home Depot sends out for delivery + delivered | Home Depot mail |
| YunExpress (Shopify store) | 1 | Yes (`YT…`) | No follow-up yet | Deep link only |
| Merchant link only (American Floor Mats) | 1 order, 3 shipments | **No** number anywhere; link to merchant status page only | No | Deep link only |

Totals: ~40 packages. About 36 (90%) reached a terminal status through email alone. The 4 that did not are UPS ×1, YunExpress ×1, link-only ×1 order, and nothing USPS (zero USPS packages in the sample).

### Findings that change the design

1. **Carrier notification mail is the best free status source, and the user already gets it.** FedEx Delivery Manager mail covered 14 of 14 FedEx packages end to end, with ETA windows. DHL On Demand covered DHL. Parsing `TrackingUpdates@fedex.com` and `NoReply.ODD@dhl.com` replaces the FedEx and DHL API adapters for this user.
2. **Walmart is two different flows, and neither involves a public carrier.** Store delivery has no number at all. Ship-to-home has a `555…` 20-digit number that is Walmart-internal. Both are fully covered by Walmart's own mail. Correlation key is the order number (`Order number: 2000NNN-NNNNNNNN` or `Order #2000NNN-NNNNNNNN`), present in text on every event mail. One order can produce several shipped mails (seen: two packages, one order).
3. **Gmail `threadId` is unusable as a correlation key.** Gmail merges unrelated Walmart mails with identical subjects into one thread: three different packages' "Your package should arrive by 6:11pm📦" landed in one thread. Correlate by tracking number, else order number, else nothing.
4. **Link harvesting does not work for Walmart and most merchants.** All 23 Walmart links are encrypted `w-mt.co` click-trackers. Whisker, HeyDude and Home Depot wrap most links too. Unwrapped carrier URLs appeared only from FedEx (`trknbr=`) and Narvar (`tracking_numbers=`). Keep the href tier, but it is a minor source.
5. **JSON-LD / microdata: 0 of 3** raw HTML bodies checked (Walmart, HeyDude, FedEx). Drop it from Phase 1; keep a cheap check only if it costs nothing.
6. **Check digits work as the main defence.** 17 of 17 real carrier numbers passed (FedEx Express 12 ×14, UPS `1Z` ×1, DHL Express ×2). 0 of 9 order and phone numbers passed; 4 of them matched a carrier regex (DHL Express 10-digit ×3, FedEx Ground/DPD ×1) and only the checksum rejected them.
7. **But sender rules must override checksums.** 1 of 3 Walmart `555…` numbers passes the USPS 20 mod10 check by chance, so a checksum-first classifier would call it USPS and poll it forever. Rule: a `help@walmart.com` "Track your package:" number is `carrier=walmart`, full stop.
8. **A labeled-field extractor is needed.** Home Depot's UPS id (`1R00…`, 28 chars) matches no jkeen pattern; it is only findable via the `Delivery by:` / `Tracking #:` labels. Whisker (`Carrier: FedEx® / Tracking number:`) and the Shopify store (`shipped via YunExpress … tracking number … is`) follow the same labeled shape.
9. **Status vocabulary observed:** Walmart `Shipped:` / `Out for delivery:` / `Arrived:` / `Delivered:` / `Your package is in transit` / `Your order is delayed ⚠️` / `Canceled: delivery of`. FedEx `on the way` / `scheduled for delivery tomorrow` / `out for delivery today` / `was delivered`. DHL `On Its Way` / `Delivery Is Today` / `ARRIVING SOON` / `Delivered!`. Home Depot `Shipped` / `Out for Delivery Today` / `Delivered`. Noise from the same senders that must be ignored: substitutions, reminders, refunds, review/survey requests, import-duty and payment notices.
10. **The Gmail connector returns `htmlBody` and link targets**, so this validation was not limited by text-only access. The app itself will use the Gmail API (`format=full`), which also returns raw HTML.

### Merchant survey (365 days, follow-up)

Second pass over non-Walmart merchants: shipping-subject search over 365 days, plus 15 bodies extracted read-only. Numbers validated with the same jkeen matcher.

| Merchant / sender | Email template | Number in mail | Carrier | Merchant sends OFD + delivered? |
|---|---|---|---|---|
| Home Depot (`HomeDepotPro@`, `HomeDepot@order.homedepot.com`) | Own; `Order # WH…/WK…/WN…` in subject | Sometimes (`Delivery by: UPS Tracking #:`, non-`1Z` id) | UPS, FedEx | Yes, ~10 orders in 60 days, all complete; "Part of your order" split shipments |
| Shopify stores (`*@t.shopifyemail.com`, and custom domains: Roborock, Insteon, TRC Electronics, GOVO, others) | Shopify stock: subject `A shipment from order X is on the way / is out for delivery / has been delivered`; body `<Carrier> tracking number: N` | Yes, labeled | FedEx, UPS, USPS | Yes, all three stages |
| B&H Photo | Own: `Tracking #1:` + `Shipping Method: Fdx Home Delivery` | Yes | FedEx | Delivered yes; FedEx mail covers the rest |
| Whisker / Litter-Robot | Own: `Carrier: FedEx® / Tracking number:` | Yes | FedEx | Yes |
| Grunt Style | Own (Shopify + Wonderment): `Shipped By USPS / Tracking #` | Yes | USPS | Yes |
| W4S | Own (Shopify + Klaviyo): `Shipped by UPS / Tracking number` | Yes | UPS | Yes |
| WhiteWall | Own: `Your shipping service: UPS / Your parcel number:`; direct `ups.com/track?tracknum=` | Yes | UPS | Shipped only (so far) |
| Tesla Shop | Own: number **only in the href** (`ups.com/track?tracknum=`) | URL only | UPS | Shipped + delivered |
| Redo (returns/tracking platform) | `Delivered 1Z…` | Yes | UPS | Delivered |
| Shippo (DrZzs) | `TRACKING NUMBER` label; links wrapped by Mandrill | Yes | USPS | Delivered; the merchant's own "shipped" mail had no number |
| Anker + Swiship (Amazon MCF) | `Tracking number: TBA… Carrier: Amazon Shipping + Amazon MCF`; Swiship mail has it only inside an `awstrack.me`-wrapped, percent-encoded URL | Yes | Amazon Logistics | Yes (Anker sends OFD + delivered) |
| eBay | `Shipped with USPS / SpeedPAK`; **no number** in mail | No | USPS, SpeedPAK | Yes (OUT FOR DELIVERY, ORDER DELIVERED) |
| Simplehuman, American Floor Mats, DrZzs (own mail) | Link to merchant status page only | No | n/a | Simplehuman delivered yes; AFM no |
| HeyDude (Narvar) | `Tracking Number:` + unwrapped `tracking.narvar.com?tracking_numbers=` | Yes | UPS | No (stalled) |
| YunExpress store | `Tracking Number: … Carrier: YunExpress` | Yes | YunExpress | No |

New validation results: 12 more real numbers, 11 pass their checksum (UPS `1Z` ×5, USPS IMpb ×5 incl. one with a `420`+ZIP routing prefix, FedEx 12 ×1); Amazon `TBA` has no checksum by design. Running totals: 28 of 28 checksum-bearing real numbers pass.

New traps:

- **Checksumless patterns fire on order numbers.** WhiteWall's 14-digit order number matches jkeen's DHL E-Commerce (14) pattern, which carries no checksum. Rule: a checksumless pattern only counts when a carrier label sits next to it, and any string labeled "Order number/Order #/Customer no." is excluded.
- **Template beats sender.** The Shopify stock template arrives from many store domains. Detect it by subject shape plus the `<Carrier> tracking number:` label, not by sender.
- **Link wrappers vary and some are decodable.** `awstrack.me/L0/<percent-encoded URL>` (Amazon SES) and Mandrill `p=<base64 JSON>` hide carrier URLs that can be decoded locally without following the redirect. Walmart `w-mt.co` and Klaviyo/Sendinblue tokens cannot.
- **Some merchants never include a number** (eBay, Simplehuman, AFM, MantelMount until shipment). These become order-level packages fed by the merchant's own status mail.

Revised carrier mix over the year: FedEx (most, fully covered by FedEx Delivery Manager mail), UPS (8 numbers, no UPS notification mail), USPS (6 numbers, no Informed Delivery mail), DHL Express (2), Amazon MCF (1), YunExpress (1), Walmart last-mile (Walmart only). UPS and USPS are the carriers where an API adds coverage.

### Universal-pattern scan (365 days, follow-up)

Method: every pattern in `jkeen/tracking_number_data` (18 couriers, 40+ formats, with their check-digit algorithms) plus extras (Walmart `555…` last-mile, labeled values after `Tracking #/Number/ID`, `Waybill`, `Parcel number`, `Pro#/ProNumber`, and tracking params in links, with Amazon SES `awstrack.me` and Mandrill `p=` wrappers decoded locally) run over the verbatim text of 203 shipping emails from 68 senders. The 203 were drawn from 884 candidates (four Gmail searches); Walmart, Home Depot and FedEx were capped at 20/20/12 because their formats repeat. The scanner is `docs/_research/assets/2026-10-10_tracking_scan.py`; it reads a local JSONL of `{id, sender, subject, body}` and passes all 20 cases in `tests/fixtures/packages/extraction_cases.json`.

| Result | Count |
|---|---|
| Emails with at least one number | 116 of 203 |
| Unique numbers found | 50: FedEx 16, UPS 12, USPS 9, DHL Express 2, Amazon TBA 2, YunExpress 2, Walmart `555…` 2, Home Depot UPS `1R00…` 4, SEFL freight PRO 1 |
| Known numbers from earlier passes recovered | 30 of 33 (the 3 misses were not in the fetched sample) |
| Candidates rejected | 30 unique numeric strings (order numbers, phone numbers, item IDs) failing checksums, plus ~130 link tokens matching no format |
| False positives before tuning | 4 of 49 (8%) |
| False positives after tuning | 0 of 50 |

The four first-pass false positives, and the rule that removed each:

| String | What it really was | Matched as | Fix |
|---|---|---|---|
| 11-digit | Merchant support phone number ("reach me at +…") | DHL Express (mod 7) | Weak validators (DHL Express mod 7: a random number passes 1 in 7; Purolator 12) only count with carrier context: DHL sender, "waybill", or "DHL" nearby |
| 10-digit | Whisker "Your Shipment #" | DHL Express (mod 7) | Same, plus `Shipment #` added to the order-label exclusions |
| 12-digit | eBay "Item ID:" | Purolator (12) | `Item ID`, `Reference ID`, `Account`, phone cues added to exclusions |
| 14-digit | eBay link `id=` parameter | DHL E-Commerce (14), no checksum | Bare `id=` only trusted on known tracking hosts (swiship.com) |

Formats not in jkeen that real mail uses: Home Depot's 28-character `1R00…` UPS id (label-only), SEFL freight 9-digit `ProNumber` (label-only, freight carrier mails its own status), Walmart `555…` (sender rule). One recall bug found and fixed: HTML-to-text output without separators glues numbers to the next word (`Tracking #: 1541729044Track`), so token boundaries must accept a following capitalised word.

Emails with no number at all (87 of 203) are, in order: Walmart store-delivery and status mail (21), eBay (4), American Floor Mats (4), B&H order-status (4, numbers sit in FedEx's mail), SEFL dispatch duplicates, Collars & Co (promotions that slipped through on "arrived"), Bose (3, status mail with no number), Simplehuman, Tesla, DrZzs, Ubiquiti, Magnus. Each is either order-level status mail or noise; none hid a number the scanner missed.

Conclusion: a single regex is not enough, but jkeen's patterns + checksums + three rules (weak validators need carrier context, labeled order/item/phone values are excluded, checksumless formats need a label) reach 50/50 precision on this mailbox, and the labeled-value tier picks up the non-standard formats jkeen does not know.

### Revised carrier priority for this user

| Priority | Path | Covers (sample) |
|---|---|---|
| 1 | Walmart mail parser (order-number keyed) | ~22 of 40 |
| 2 | Carrier notification mail parsers: FedEx Delivery Manager, DHL On Demand | 16 of 40 |
| 3 | Generic merchant parser (labeled fields + jkeen + href) for "shipped" mail | creates the package for the rest |
| 4 | UPS Track and USPS Tracking v3 APIs (opt-in keys) | merchants that stop after "shipped" (HeyDude, WhiteWall); 8 UPS + 6 USPS numbers in 365 days |
| 5 | Deep link + `stale` after N days | YunExpress, link-only merchants |
| Dropped | FedEx API, DHL API adapters | FedEx and DHL fully covered by their own notification mail |

## Dissent / Contradictory Evidence

- **Email-only vs API polling.** The contrarian pass argues email-derived status is uneven (late or missing "delivered" mail from small merchants). That is why carrier APIs stay in scope for the four big carriers. Conversely, carrier APIs add per-carrier signup friction (FedEx account link, USPS BCG, UPS company fields) for a personal tool. Resolution: make APIs opt-in per carrier, ship email-derived first, measure how often packages stall in `in_transit`.
- **JSON-LD value.** library-docs called ParcelDelivery markup anecdotally common among large merchants; web-researcher found Gmail markup requires sender registration and no adoption stats. Both agree: parse it when present, never depend on it.
- **USPS quota and access rules.** USPS's FAQ confirms 429 and quota requests but not the 60/hr figure, which comes from third-party packages; the April 2026 Access Control change could alter individual eligibility. Verify at signup before building the adapter.
- **LLM extraction.** Codebase already ships Anthropic Haiku and an extraction pattern, so it is cheap to add. It contradicts "open and free" (third-party processing of order mail). Kept optional, off by default.

## Recommendation

### Decision

Revised 2026-10-10 after the Gmail validation above. Ship a `packages` feature whose status comes from email first, with one optional API:

- **Phase 1 (no new credentials):** Gmail scanner over three parser families:
  - Walmart (`help@walmart.com`): store delivery and `555…` last-mile, keyed by order number.
  - Carrier notification mail: FedEx Delivery Manager (`TrackingUpdates@fedex.com`) and DHL On Demand (`NoReply.ODD@dhl.com`).
  - Shopify stock template (detected by subject shape, any sender) and Home Depot.
  - Generic merchant mail: labeled fields first (incl. `Pro#`/`ProNumber` for freight), then vendored jkeen regex with check digits (checksumless and weak-checksum patterns such as DHL Express mod 7 and Purolator only beside carrier context; order/item/phone-labeled values excluded), then carrier URLs including locally decoded `awstrack.me` and Mandrill wrappers. Merchants with no number become order-level packages fed by their own status mail.

  Plus a monotonic status state machine, sender-over-checksum carrier rules, order-number correlation (never `threadId`), and a `/packages` page with deep links, manual add and archive.
- **Phase 2 (opt-in keys):** UPS Track and USPS Tracking v3 adapters behind the `CarrierAdapter` protocol (revised after the 365-day merchant survey: UPS ×8 and USPS ×6 numbers, neither with carrier notification mail). FedEx and DHL adapters stay out while their notification mail covers them.

Explicitly out: scraping, Google search, paid or freemium aggregators, Karrio, JSON-LD parsing, LLM extraction by default, and FedEx/DHL API adapters for now.

### Rationale

- Measured on the user's mailbox: about 90% of packages reach delivered through Walmart, FedEx and DHL mail alone. Email-first covers the dominant merchant (Walmart, whose numbers no carrier API knows) and needs zero signup.
- Official APIs are free, stable, and documented; scraping is neither stable nor polite; aggregators are neither free at volume nor durable.
- Check digits rejected 9 of 9 order/phone look-alikes and accepted 17 of 17 real numbers; sender rules fix the one observed checksum collision (Walmart `555…` vs USPS 20).
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

Walmart handling (superseded in part by Validation finding 2, which shows Walmart's ship-to-home numbers are its own last-mile, not a carrier's): a dedicated parser for `walmart.com` senders. Ship-to-home mail: pull carrier + tracking number (text and links), one order can yield several packages. Store delivery mail: no tracking number, so key on `order_id` + `threadId` (carrier `walmart`, synthetic number `order:<id>`), status from Walmart's own mail only. Walmart Marketplace third-party sellers ship with their own carriers and go through the generic path.

Status chip mapping (DESIGN.md: icon + label): pending `schedule`, in_transit `local_shipping`, out_for_delivery `near_me`, delivered `check_circle`, exception `error`, returned `undo`, stale `help`.

Optional follow-up: `GET /api/packages/summary` for a Home Assistant REST sensor (count out for delivery today), and a digest section for packages delivered or arriving today via `pipeline/collectors/__init__.py` dispatch.

### Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Numeric false positives (order/phone numbers) | High without checks | Check digit + keyword proximity + per-sender suppress list; fixture corpus with precision assertions |
| Walmart store-delivery status lag or missing "delivered" mail | Medium | Show `last_event_at` age; mark `stale`; deep link to the Walmart order page |
| Walmart mail format unverified (tracking number in text vs only behind a walmart.com order link) | Medium | Build the Walmart parser from a redacted sample of the user's own Walmart mail before writing rules |
| Carrier signup friction (FedEx account link, USPS BCG) | Medium | Adapters opt-in; Phase 1 useful without them |
| USPS/UPS terms or quota change | Medium | Adapter isolation, 429 backoff, `last_error` surfaced per carrier on the page |
| Click-wrapped tracking links | Medium | Decode embedded targets; never auto-follow; fall back to regex |
| Gmail token expiry | Low (already mitigated) | Existing Production consent screen; reuse health checks |

## Open Questions

1. USPS April 2026 "API Access Control": does an individual BCG account still get Tracking v3? Verify at signup.
2. UPS: does Track require an account number on the app? Docs page returned 503 during research.
3. FedEx: confirm a free personal fedex.com account satisfies the project's account-link requirement.
4. Extractor precision: measured on a small sample (see Validation, findings 6 to 8). Re-measure on a full 180-day pass once the scanner exists.
6. Does the user have UPS My Choice? Enrolling (free) would give UPS the same email coverage FedEx has and remove the need for the Phase 2 UPS key.
5. Should packages appear in the daily reMarkable digest? Product call, not a research gap.

## Sources

See frontmatter `citations`. Codebase claims cite `path:line`, verified on branch `claude/great-johnson-7qeay0` at `c966b01`.
