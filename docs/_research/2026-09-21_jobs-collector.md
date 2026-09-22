---
citations:
  - url: "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=false"
    title: "Greenhouse Job Board API — public list endpoint (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200, {\"jobs\":[{\"absolute_url\":...}]}; unknown board token returns 404"
    status: LIVE
  - url: "https://docs.greenhouse.io/job-board.html"
    title: "Greenhouse Job Board API docs"
    pub_date: "living"
    fetched_ts: null
    claimed_span: "GET boards/{token}/jobs public; POST application requires HTTP Basic auth with the employer's Job Board API key"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://github.com/grnhse/greenhouse-api-docs/blob/master/source/includes/job-board/_applications.md"
    title: "Greenhouse Job Board API — applications"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:50:00Z"
    claimed_span: "application POST authenticated with the employer's API key (Basic auth)"
    status: LIVE
  - url: "https://github.com/lever/postings-api"
    title: "Lever Postings API"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:50:00Z"
    claimed_span: "GET /v0/postings/{site} public; POST /v0/postings/{site}/{id}?key= requires an employer-issued API key"
    status: LIVE
  - url: "https://api.lever.co/v0/postings/palantir?mode=json&limit=1"
    title: "Lever postings — public list endpoint (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200, JSON array of postings"
    status: LIVE
  - url: "https://developers.ashbyhq.com/docs/public-job-posting-api"
    title: "Ashby Public Job Posting API"
    pub_date: "living"
    fetched_ts: null
    claimed_span: "GET posting-api/job-board/{clientname}?includeCompensation=true, unauthenticated"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
    title: "Ashby job board — public list endpoint (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200, {\"jobs\":[{\"id\":...,\"title\":...,\"employmentType\":\"FullTime\",...}]}"
    status: LIVE
  - url: "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    title: "Workday CXS jobs endpoint (undocumented; live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "POST {\"appliedFacets\":{},\"limit\":1,\"offset\":0,\"searchText\":\"\"} -> 200 {\"total\":2000,\"jobPostings\":[{\"title\":...,\"externalPath\":...,\"postedOn\":\"Posted Today\"}]}"
    status: LIVE
  - url: "https://developers.smartrecruiters.com/docs/posting-api"
    title: "SmartRecruiters Posting API"
    pub_date: "living"
    fetched_ts: null
    claimed_span: "officially documented public postings list; application creation needs API key/OAuth2 client credentials"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://api.smartrecruiters.com/v1/companies/boschgroup/postings?limit=1"
    title: "SmartRecruiters postings — public list endpoint (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:57:00Z"
    claimed_span: "HTTP 200 {\"offset\":0,\"limit\":1,\"totalFound\":4806,...}"
    status: LIVE
  - url: "https://apply.workable.com/api/v1/widget/accounts/huggingface"
    title: "Workable widget JSON (undocumented; live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200 {\"name\":\"Hugging Face\",\"description\":...}"
    status: LIVE
  - url: "https://ats.rippling.com/api/v2/board/rippling/jobs"
    title: "Rippling ATS board JSON (undocumented; live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200 {\"items\":[{\"id\":...,\"name\":\"Business Operations Manager\",\"url\":\"https://ats.rippling.com/rippling/jobs/...\",\"department\":...,\"locations\":[...]}]}"
    status: LIVE
  - url: "https://zapier.bamboohr.com/careers/list"
    title: "BambooHR careers list JSON (undocumented; live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:57:00Z"
    claimed_span: "HTTP 200 {\"meta\":{\"totalCount\":0},\"result\":[]} (JSON shape confirmed; this tenant had no openings)"
    status: LIVE
  - url: "https://bunq.recruitee.com/api/offers/"
    title: "Recruitee offers JSON (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200 {\"offers\":[{\"careers_apply_url\":...,\"hybrid\":true,\"title\":...}]}"
    status: LIVE
  - url: "https://personio.jobs.personio.de/xml"
    title: "Personio XML job feed (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:55:00Z"
    claimed_span: "HTTP 200 <workzag-jobs><position><id>..."
    status: LIVE
  - url: "https://yc-oss.github.io/api/companies/hiring.json"
    title: "yc-oss/api — YC companies currently hiring (static JSON mirror; live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:57:00Z"
    claimed_span: "HTTP 200, 2.5 MB JSON array; entries carry name, slug, website"
    status: LIVE
  - url: "https://github.com/yc-oss/api"
    title: "yc-oss/api repository"
    pub_date: "living"
    fetched_ts: null
    claimed_span: "daily GitHub Actions refresh of YC's public company directory into static JSON"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=1"
    title: "HN Algolia API — 'Who is hiring' threads (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:57:00Z"
    claimed_span: "HTTP 200, hits authored by whoishiring"
    status: LIVE
  - url: "https://remotive.com/api/remote-jobs?limit=1"
    title: "Remotive public API (live probe)"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:57:00Z"
    claimed_span: "HTTP 200 JSON; response carries a legal notice field"
    status: LIVE
  - url: "https://github.com/plie/em-job-poller"
    title: "em-job-poller — curated company→ATS slug list, polls Greenhouse/Lever/Ashby"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:52:00Z"
    claimed_span: "~90 company->ATS-slug mappings in companies.json"
    status: LIVE
  - url: "https://github.com/Feashliaa/job-board-aggregator"
    title: "job-board-aggregator — claims 20k+ companies across Greenhouse/Lever/Ashby/Workday"
    pub_date: "living"
    fetched_ts: null
    claimed_span: "claimed scale unverified"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool"
    title: "Claude web search tool"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:52:00Z"
    claimed_span: "$10 per 1,000 searches plus standard token costs; max_uses, allowed_domains/blocked_domains"
    status: LIVE
  - url: "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool"
    title: "Claude web fetch tool"
    pub_date: "living"
    fetched_ts: "2026-09-22T03:52:00Z"
    claimed_span: "no additional charge beyond token costs; cannot render JavaScript; max_content_tokens"
    status: LIVE
  - url: "https://developers.hubspot.com/changelog/upcoming-sunset-of-clearbits-free-logo-api"
    title: "Sunset of Clearbit's free Logo API (HubSpot changelog)"
    pub_date: "2025"
    fetched_ts: null
    claimed_span: "Clearbit free logo API sunset in 2025"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://support.greenhouse.io/hc/en-us/articles/115005448066-Invisible-reCAPTCHA"
    title: "Greenhouse — Invisible reCAPTCHA"
    pub_date: "living"
    fetched_ts: null
    claimed_span: "invisible reCAPTCHA built into standard careers-page application forms"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://natlawreview.com/article/linkedin-s-data-scraping-battle-hiq-labs-ends-proposed-judgment"
    title: "LinkedIn's data scraping battle with hiQ Labs ends (NatLawReview)"
    pub_date: "2022-12"
    historical: "primary coverage of the hiQ settlement; the event is historical"
    fetched_ts: null
    claimed_span: "hiQ paid $500,000, permanent injunction; User Agreement enforceable as contract"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://www.proskauer.com/blog/taking-cue-from-the-supreme-courts-van-buren-decision-ninth-circuit-releases-new-opinion-holding-scraping-of-publicly-available-website-data-falls-outside-of-cfaa"
    title: "Ninth Circuit: scraping public website data falls outside CFAA (Proskauer)"
    pub_date: "2022-04"
    historical: "legal precedent; the ruling is historical"
    fetched_ts: null
    claimed_span: "scraping publicly available data generally not a CFAA violation post-Van Buren"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://jobspipe.dev/blog/indeed-publisher-api"
    title: "Indeed Publisher API shutdown (jobspipe.dev)"
    pub_date: "2025"
    fetched_ts: null
    claimed_span: "Indeed publisher API shut down (2023), no new keys issued"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://blog.loopcv.pro/what-happened-to-sonara/"
    title: "What happened to Sonara (LoopCV)"
    pub_date: "2024"
    fetched_ts: null
    claimed_span: "Sonara shut down; assets went to the BOLD portfolio"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://www.herohunt.ai/blog/surviving-the-ai-application-flood-2026-playbook/"
    title: "Surviving the AI application flood (HeroHunt)"
    pub_date: "2026"
    fetched_ts: null
    claimed_span: "recruiter backlash to AI mass-applications (specific ban/interview-rate figures NOT found on page by critic)"
    status: LIVE  # probed by research-critic 2026-09-22
  - url: "https://www.sardine.ai/blog/job-application-fraud-detection"
    title: "Job application fraud detection (Sardine)"
    pub_date: "2025"
    fetched_ts: null
    claimed_span: "vendors selling detection of fake/mass-generated/bot job applications"
    status: LIVE  # probed by research-critic 2026-09-22
---

# Research: Jobs Collector — company research, ATS board polling, per-ATS apply action items

**Date**: 2026-09-21
**Type**: Feature Investigation
**Status**: Complete
**Stack**: Python 3.12, FastAPI, SQLModel/SQLite, APScheduler, `requests` + `httpx`, Anthropic SDK (triage); Nuxt/Vue web UI

---

## Summary

A jobs collector should get postings **straight from employers' own applicant tracking systems (ATS)**, not from LinkedIn or Indeed. Ten ATS platforms expose unauthenticated JSON (or XML) listing endpoints. Each was live-probed on 2026-09-22 and returned 200: Greenhouse, Lever, Ashby, Workday (the CXS endpoint behind the careers page), SmartRecruiters, Workable, Rippling, BambooHR, Recruitee and Personio. Five of them return a clean 404 for an unknown slug, so the tool can resolve company → board by guessing slugs cheaply. LinkedIn has no job-search API and enforces its ban on automated access through its User Agreement (hiQ paid $500k despite winning on the CFAA question). Indeed's publisher feed was shut down in 2023. **No ATS lets a candidate submit an application by API.** Greenhouse, Lever and SmartRecruiters have apply APIs, but they need the *employer's* key. Workday needs a separate candidate account on every tenant. Everyone else only offers a hosted form. Bot defenses (reCAPTCHA, hCaptcha, vendor-sold detection of mass-generated applications) are getting stronger. **Recommendation:** build discovery → resolution → polling → fit scoring as a stateful subsystem modeled on the domain collector. Make "apply" a **per-ATS action item** (a prepared packet plus a checklist, and later a browser assist that fills the form but never submits it). Do not automate submission.

---

## Research Questions

### Q1: Where do job postings come from, beyond LinkedIn and job sites?
**Answer**: Employers' own ATS boards are the authoritative source and the most accessible one. Ten platforms have public listing endpoints, all verified live (Findings §ATS endpoints). A few aggregators also work: Remotive and the HN "Who is hiring" threads via the Algolia API (both verified live). USAJobs works but needs a key (anonymous request returned 403). Adzuna and Jooble have keyed publisher programs, but only vendor blogs say so; not verified. LinkedIn has no API, and Indeed shut down its publisher feed in 2023 (the contrarian agent reported a 2025 ZipRecruiter shutdown too; its cited source does not mention it, so unverified).

### Q2: How does the collector "research companies" and find their boards?
**Answer**: It runs a resolver ladder, cheapest step first:
1. Known ATS URL patterns in the careers link or redirect chain.
2. Slug probes against the five ATS APIs that return a clean 404.
3. Fingerprinting of the homepage and `/careers` HTML (embed scripts, iframes, `*.myworkdayjobs.com`).
4. Claude `web_search` + `web_fetch` (about $0.01 per search plus tokens; fetch has no fee beyond tokens).
5. schema.org `JobPosting` JSON-LD as the last fallback.

Seed companies come from a manual watchlist, `yc-oss` `hiring.json` (verified), HN hiring threads, and companies named in job-alert emails the existing Gmail and M365 collectors already ingest.

### Q3: Can applications be submitted by API for Workday, Rippling, Greenhouse, etc.?
**Answer**: No. The only apply APIs need employer credentials (Greenhouse Basic auth with the employer's key, Lever `?key=` issued by the employer's Super Admin, SmartRecruiters API key or OAuth2). Workday, Oracle and SuccessFactors need a candidate account per tenant. Ashby, Workable, Rippling, BambooHR, Recruitee, Teamtailor, JazzHR, Jobvite and iCIMS only have hosted forms. "Apply" therefore has to be a human action, supported by a prepared packet.

### Q4: Is automated submission worth building later?
**Answer**: Not as unattended submission. Hosted forms use invisible reCAPTCHA (Greenhouse) or hCaptcha (Lever). Fraud vendors now sell detection of mass or bot applications to ATS customers (Sardine), and device-fingerprint vendors target exactly the single-machine, many-employer pattern this tool would produce. The commercial auto-apply track record is poor (Sonara shut down and its assets went to the BOLD portfolio; LazyApply draws complaints about wrong answers on screening questions). A *fill-but-don't-submit* browser assist is the most automation worth building.

### Q5: How does this fit SignalSlate's architecture?
**Answer**: It fits the domain-collector precedent. Real work runs on its own scheduler job with its own tables. The collector registered in `dispatch()` only diffs stored state into digest items. Fit scoring reuses `make_client()`/`llm_settings()` the way `pipeline/domains/ideas.py` does. The apply lifecycle mirrors `DomainPurchase`: a pending → done state machine with an idempotency key and a confirmation step. There is no action-item model yet, so a new table is needed.

### Q6: Legal/ToS posture?
**Answer**: Polling public ATS JSON is low-risk. Van Buren and the 9th Circuit hiQ remand put public data outside the CFAA, and a commercial market of Greenhouse/Lever/Ashby scrapers operates openly. The live risk is **contract (ToS)**, not criminal law. Keep LinkedIn and Glassdoor out entirely, poll politely (daily, honor 429/`Retry-After`), and keep submission human so the tool never makes representations to an employer on the user's behalf.

---

## Findings

### ATS endpoints (live-probed 2026-09-22)

| ATS | List endpoint | Auth | Unknown slug | Board id in careers URL | Apply channel |
|---|---|---|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | none | 404 | `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}`, embed `?for={token}` | hosted form (reCAPTCHA); API needs employer key |
| Lever | `GET api.lever.co/v0/postings/{site}?mode=json` (EU: `api.eu.lever.co`) | none | 404 | `jobs.lever.co/{site}` | hosted form (hCaptcha); API needs employer key |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | none | 404 | `jobs.ashbyhq.com/{name}` | hosted form only |
| Workday | `POST {tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` body `{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}` | none | n/a (needs tenant+site+wdN) | `{tenant}.wd{N}.myworkdayjobs.com/{site}` | **account per tenant** + email verification |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{id}/postings` | none (officially documented) | returns `totalFound:0` rather than 404 | `jobs.smartrecruiters.com/{id}` | hosted form; API needs employer creds |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{account}` | none | 404 | `apply.workable.com/{account}` | hosted form |
| Rippling | `GET ats.rippling.com/api/v2/board/{slug}/jobs` | none | 404 | `ats.rippling.com/{slug}/jobs` | hosted form |
| BambooHR | `GET {co}.bamboohr.com/careers/list` (`Accept: application/json`) | none | not tested | `{co}.bamboohr.com/careers` | hosted form |
| Recruitee | `GET {co}.recruitee.com/api/offers/` | none | not tested | `{co}.recruitee.com` | hosted form (`careers_apply_url` in payload) |
| Personio | `GET {co}.jobs.personio.de/xml` | none | not tested | `{co}.jobs.personio.{de,com}` | hosted form |
| Teamtailor, JazzHR, Jobvite, iCIMS, Taleo/Oracle, SuccessFactors | no public JSON verified | — | — | `{co}.teamtailor.com`, `{co}.applytojob.com`, `jobs.jobvite.com/{co}`, `*.icims.com`, `*.taleo.net` / `*.oraclecloud.com/hcmUI/CandidateExperience`, `*.career.successfactors.*` | hosted form / account portal → JSON-LD or HTML fallback |

Evidence: live `curl` probes (citations in frontmatter); Greenhouse apply-auth and Lever postings-api docs fetched by library-docs agent.
- Workday's CXS endpoint is **undocumented**: it is the call the careers SPA itself makes. Our probe returned `total: 2000` for a large tenant, which suggests a result cap. Community reports put the page size at ~20 and an offset ceiling near 10k (unverified). Build it defensively.
- Rippling v2 list items carry `id, name, url, department, locations`. Library-docs cited third-party reports that the list has no description, so expect a per-job detail fetch.
- Only Lever documents a rate limit (2 req/s on apply POST). Nobody publishes read limits, so once per day per board is the safe default.

### Company discovery sources

- **Manual watchlist**: primary input; name + domain, optional careers URL hint.
- **yc-oss `companies/hiring.json`**: static JSON mirror of YC's directory, refreshed daily via GitHub Actions. Verified 200, 2.5 MB, has `website`. Free, and avoids hitting YC's Algolia directly.
- **HN "Who is hiring"** via `hn.algolia.com/api/v1` (`author_whoishiring`): verified live. Monthly thread; comments name companies and often link ATS URLs directly.
- **Own inbox**: the Gmail and M365 collectors already ingest mail. Job-alert emails (LinkedIn, Indeed, Wellfound alerts the user subscribed to) name companies and titles. Extracting company names from *the user's own mail* gets LinkedIn-discovered roles without touching LinkedIn, then resolves them to the employer's ATS. (Synthesis from `pipeline/collectors/gmail.py` + ToS findings; not an external citation.)
- **Remotive** API (verified live) and keyed **USAJobs** (403 without key; free key) as optional aggregator sources.
- **Skip**: Crunchbase (no free tier in 2025+, $49–99/mo, vendor-blog sourced); Clearbit logo API (sunset 2025); Wellfound/BuiltIn/levels.fyi (no API; scraping is against ToS). Wikidata SPARQL and SEC EDGAR are free but low-yield for "who's hiring"; defer.

### Resolution (company → board)

1. **URL pattern match** on any URL already known (careers hint, HN comment link, email link, redirect chain) against the host table above. Deterministic and free.
2. **Slug probe**: derive candidates from name/domain (`acme`, `acmeinc`, `acme-corp`, domain SLD) and GET the Greenhouse/Lever/Ashby/Rippling/Workable list endpoints. A 404 means a miss. Verify a hit by matching the returned company name or website. At most 5 platforms × 3 slugs = 15 cheap GETs per company.
3. **HTML fingerprint**: fetch the homepage, follow the `careers|jobs` link, then scan HTML and redirects for ATS hosts and embed scripts (`boards.greenhouse.io/embed/job_board/js?for=`, `jobs.lever.co`, `jobs.ashbyhq.com`, `myworkdayjobs.com`). This is the only way to recover Workday's tenant + `wdN` + site.
4. **Claude research step**: `web_search` (`$10/1k`) + `web_fetch` (tokens only; cannot render JS), with `max_uses` and `max_content_tokens` caps, returning structured `{careers_url, ats_kind, board_id, confidence}`. It runs only when steps 1–3 miss. Cost is roughly cents per company.
5. **JSON-LD fallback**: `"@type":"JobPosting"` blocks via `extruct` (unverified lib claim), for custom career sites.

Store every resolution with `method` + `confidence` + `verified_at`, so the UI can show "auto-detected via slug probe" and the user can correct it.

### Reusable OSS / datasets

- `plie/em-job-poller` (fetched): ~90 curated company→slug mappings. The discovery agent also reported that it hashes postings with the description excluded (because ATS rewrite whitespace and tracking params on every poll). The critic did not find that in the README, so it is unverified, but the rule is still sound to adopt.
- `Feashliaa/job-board-aggregator`: claims 20k+ company→board mappings (unverified). If its dataset is licensed for reuse, it could seed a "suggest companies" import. Inspect the license before use.
- Other small scrapers (`plibither8/jobber`, `adgramigna/job-board-scraper`, `d-alleyne/ashby-job-scraper`) confirm the endpoint shapes; no dependency is worth taking. Each adapter is ~50–100 lines on `requests`.

### Dedup / change detection

- Canonical key when polling ATS directly: `(ats_kind, board_id, external_job_id)`. Stable and never merges by accident.
- Cross-source key (email alert / HN / aggregator → ATS): `norm(company)::norm(title)::norm(city)`. Blank location acts as a wildcard; keep the req id as a tiebreaker. Known failure modes: dropping multi-city postings, and over-merging distinct reqs (issues cited by discovery agent; unverified).
- Lifecycle like `Domain`: `first_seen`, `last_seen`, `closed_at`, where closed means missing from N consecutive polls (not 1, to absorb transient errors).

### Codebase fit (verified)

- `pipeline/collectors/__init__.py:79` `dispatch(source, since, until)`: pure, no DB, routes by source id (`if source == "domains": return collect_domains(...)` at :93). Add `if source == "jobs": return collect_jobs(...)`.
- `pipeline/collectors/domains.py` makes no network calls. It diffs stored snapshots into `domain_alert` items for the digest window. `collect_jobs()` should do the same: emit `job_new` / `job_closed` / `application_update` items from stored state.
- `api/scheduler.py:52` `DOMAINS_JOB_ID`, `:64` `_run_domains_scheduled`, `:101` `reschedule_domains`: template for `JOBS_JOB_ID` + its own cron.
- `pipeline/db.py:119` `Domain` (state row with first/last seen) and `:173` `DomainPurchase` (pending/succeeded/failed/unknown, UNIQUE idempotency key): templates for `JobPosting` and `JobApplication`.
- `pipeline/domains/ideas.py` reuses `pipeline.triage.make_client()` with its own schema and prompt. Job fit scoring follows the same pattern. `triage.py:35` `BATCH_SIZE=20` and structured `messages.parse(output_format=...)` apply directly.
- `pipeline/config_store.py:18` `"tracker": "mstodo"  # mstodo | todoist | none`: an external todo sink is planned but not built. **No local action-item model exists** (codebase-analyst grep for `todo|action_item|ActionItem` across `pipeline/` and `api/`).
- Tests use `monkeypatch` + hand-rolled fakes, not respx; DB isolation by swapping `db.engine` (`tests/test_api_domain_buy.py:48-50`). Domain code uses `requests` (`pipeline/domains/intel.py:34`), with `_sleep` indirection and `_RETRYABLE_STATUS`.
- UI convention: `web/pages/<feature>.vue`, `web/components/<Feature>*.vue`, `web/composables/use<Feature>Api.ts`.

---

## Dissent / Contradictory Evidence

- **"Apply automation is the point"** vs the evidence. The user wants eventual action items for Workday, Rippling, etc. The contrarian agent's case against auto-submit is strong: CAPTCHA hardening, vendor fraud detection, fingerprint vendors, hiQ contract exposure, and Sonara/LazyApply failures. But the recruiter-backlash and interview-rate figures (26% workload increase, 33.5%/19.6% detection, 5.75% vs 2.68%) were attributed to **one vendor blog** (herohunt.ai), and the critic did not find them on that page (nor the claimed Mar 2025 LinkedIn bans of Apollo.io/Seamless.ai). Treat them as unsupported. Also unsupported: Ashby's Sept 2025 fraud detection (not on the cited Sardine page), ZipRecruiter's 2025 feed shutdown (not on the cited jobspipe page) and Sonara's Feb 2024 date. The *direction* is corroborated by independent sources (Greenhouse reCAPTCHA doc, Sardine, hiQ coverage); the numbers are not. The decision below rests on the verified structural facts (no candidate apply API exists; forms are CAPTCHA-protected), not on those stats.
- **Undocumented endpoints.** Workday CXS, Workable widget, Rippling v2, BambooHR and Recruitee are not vendor-published APIs. They work today (verified) but can change without notice. The library-docs agent called Rippling "no official endpoint found" and its list payload "no description". Our probe found a working `api/v2/board/{slug}/jobs` JSON endpoint, so that part of the agent's finding is superseded. Description availability was not checked.
- **Aggregator liveness.** The contrarian agent's claim that Adzuna/Jooble/Talent.com "still run publisher programs" came from job-data vendors with a commercial incentive. Not verified; treated as optional sources only.
- **Seed datasets.** The discovery agent recommended seeding from `job-board-aggregator`'s 20k list; its scale and license are unverified. The manual watchlist plus resolver stays the primary path.

---

## Citation Health

Probed by research-critic 2026-09-22T04:05Z. Verdict **PASS**: 30 LIVE, 1 UNKNOWN (Workday CXS is POST-only, so GET returns 400 as expected). Five claims were not supported by their cited pages and have been corrected above: the ZipRecruiter 2025 shutdown, Ashby's Sept 2025 fraud detection, the Sonara Feb 2024 date and relaunch, em-job-poller's description-excluded hash, and the herohunt ban and interview-rate stats. None of them feeds the Decision.

| Source group | Status |
|---|---|
| ATS live endpoints (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Rippling, BambooHR, Recruitee, Personio) | LIVE |
| Workday CXS | UNKNOWN (POST-only; our POST probe returned 200) |
| Vendor docs (Greenhouse, Lever, Ashby, SmartRecruiters, Claude web tools, HubSpot/Clearbit, Greenhouse reCAPTCHA) | LIVE |
| Discovery (yc-oss, HN Algolia, Remotive, em-job-poller, job-board-aggregator) | LIVE |
| Legal/community (NatLawReview, Proskauer, jobspipe, loopcv, herohunt, sardine) | LIVE (content partially ungrounded, see above) |

---

## Compatibility Analysis

### Stack Compatibility

| Aspect | Status | Notes |
|--------|--------|-------|
| Python / FastAPI / SQLModel | Compatible | New tables in `pipeline/db.py`, same `create_all` path as `Domain*` |
| HTTP | Compatible | `requests==2.34.2` and `httpx==0.28.1` both pinned; follow domain code (`requests`, `_sleep` indirection) |
| Scheduler | Compatible | Third APScheduler job alongside `JOB_ID` / `DOMAINS_JOB_ID` |
| LLM | Compatible | Anthropic SDK already used; web_search needs org-level enablement in Console (discovery agent, from Claude docs) |
| Browser assist (phase 3) | Requires new dep | Playwright + Chromium; heavier Docker image. Optional, off by default |
| Existing dependencies | No conflicts | Optional `extruct` for JSON-LD; everything else is stdlib + `requests` |

### Integration Complexity

- **Effort estimate**: High (3+ days) overall. MVP (discover + resolve + poll + score + UI) ≈ domain-collector scale minus purchase/registrar-auth. Apply action items are a separate phase.
- **Files affected**: MVP ~20–25 new or changed files; phase 2 ~8–12 more. Counted by the codebase agent by mapping each piece onto `docs/plans/domain-collector/tasks.json` T-001..T-030 and the files those tasks produced.
- **Breaking changes**: No. Additive tables, new router, new nav tab, new `dispatch()` branch.
- **Migration path**: None (greenfield).

---

## Recommendation

### Decision

Build a **stateful jobs subsystem** (`pipeline/jobs/`) modeled on the domain collector, in three phases.

1. **Discover & poll (MVP)**: company watchlist + seed importers (yc-oss, HN hiring, job-alert emails) → resolver ladder (URL pattern → slug probe → HTML fingerprint → Claude web research → JSON-LD) → per-ATS read adapters for Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Rippling, BambooHR, Recruitee, Personio → `JobPosting` state table with first/last seen/closed → LLM fit scoring against a `JobsProfile` → `collect_jobs()` digest items + a `web/pages/jobs.vue` list.
2. **Apply action items**: `JobApplication` + per-ATS **apply playbooks**. Each `ats_kind` declares `apply_mode` (`hosted_form` | `account_per_tenant` | `email`) and a step checklist; for example, Workday: "create account on `{tenant}` with {email} → verify email → upload resume → re-check parsed work history → answer EEO → submit". Also a Claude-generated **packet**: tailored resume bullets, cover note, answers to common screening questions. Status moves `saved → preparing → ready → submitted(by user) → interviewing/rejected/closed`. The user marks the item submitted and it becomes a digest todo (later pushed to the `tracker` sink).
3. **Apply assist (guided session)**: the system opens a visible Chrome window on the user's desktop and walks the playbook checklist with the user. At each step it reads the page, fills every field it can (contact info, work history, links, screening answers), generates and attaches the cover letter and resume, and clicks Next/Continue. It stops at CAPTCHAs, legal attestations and the final Submit, which stay with the user. Design in §Apply Assist. Application status can also be inferred from confirmation and rejection emails already flowing through the Gmail/M365 collectors.

No LinkedIn/Indeed/Glassdoor scraping. No unattended submission.

### Rationale

- The ATS boards are the source of truth. Ten platforms verified live without auth, and they cover the platforms the user named (Workday, Rippling) plus the startup-dominant ones (Greenhouse, Lever, Ashby).
- A candidate apply API does not exist on any ATS. Every submit path is an employer key, a per-tenant account, or a CAPTCHA-protected hosted form. Action items are the only design that works on all of them.
- The domain collector already proved the pattern in this repo (own job, own tables, a diff-only `dispatch()` collector, confirm-gated state machine). Reusing it keeps the runner/digest contract untouched.
- Cheap resolution first (pattern + slug probes are free and deterministic); LLM web research only on misses keeps Claude spend in cents per company.
- Human-in-the-loop submission avoids the contract/ToS exposure, the bot-detection arms race and the spray-and-pray penalty in one decision.

### Comparison Matrix

| Criteria | ATS-direct + action items (recommended) | Aggregator APIs only | Full auto-apply bot |
|---|---|---|---|
| Coverage of named ATS (Workday, Rippling, …) | High | Low (Indeed/Zip gone, LinkedIn none) | High in theory |
| Data freshness / authority | Source of truth | Delayed, second-hand | Source of truth |
| Legal / ToS risk | Low (public JSON, human submits) | Low | High (contract exposure, CAPTCHA evasion) |
| Reliability | Medium (undocumented endpoints drift) | Medium | Low (CAPTCHA, form drift, per-tenant accounts) |
| Cost | ~free + cents of Claude per resolution | Keys, some paid | Captcha farms, proxies |
| Outcome quality | Tailored, human-reviewed | n/a | Spray-and-pray |
| **Overall** | **Recommended** | Supplementary only | Not recommended |

---

## Implementation Sketch

### Step 1: Tables (`pipeline/db.py`)

```python
class JobCompany(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    domain: Optional[str] = Field(default=None, index=True)
    source: str                     # watchlist | yc | hn | email | manual
    status: str = "active"          # active | muted
    first_seen: datetime
    last_seen: datetime

class JobBoard(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="jobcompany.id", index=True)
    ats_kind: str                   # greenhouse | lever | ashby | workday | ...
    board_id: str                   # token/site/slug; workday: "tenant|wdN|site"
    resolved_by: str                # pattern | slug_probe | html | llm | manual
    confidence: float
    verified_at: Optional[datetime] = None
    last_polled_at: Optional[datetime] = None
    last_error: Optional[str] = None
    # UNIQUE(ats_kind, board_id)

class JobPosting(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(foreign_key="jobboard.id", index=True)
    external_id: str                # ATS job id; UNIQUE(board_id, external_id)
    title: str
    location: Optional[str] = None
    remote: Optional[bool] = None
    comp_text: Optional[str] = None
    apply_url: str
    content_hash: str               # excludes description whitespace/tracking noise
    first_seen: datetime
    last_seen: datetime
    closed_at: Optional[datetime] = None
    missed_polls: int = 0
    fit_score: Optional[int] = None # 0-100
    fit_reason: Optional[str] = None
```

`JobsProfile` is a singleton row: target roles, locations, remote preference, salary floor, resume text, exclusions. Phase 2 adds `JobApplication` (posting_id UNIQUE, status, playbook progress JSON, packet JSON, submitted_at), mirroring `DomainPurchase`.

### Step 2: ATS adapters (`pipeline/jobs/ats/`)

```python
class AtsAdapter(Protocol):
    kind: str
    apply_mode: str                            # hosted_form | account_per_tenant
    def match_url(self, url: str) -> Optional[str]: ...        # -> board_id
    def probe(self, slug: str) -> Optional[BoardInfo]: ...     # None on 404
    def list_postings(self, board_id: str) -> list[RawPosting]: ...
```

One module per ATS (`greenhouse.py`, `lever.py`, `ashby.py`, `workday.py`, `smartrecruiters.py`, `workable.py`, `rippling.py`, `bamboohr.py`, `recruitee.py`, `personio.py`) plus a `REGISTRY`. Use `requests` with a descriptive User-Agent, `_RETRYABLE_STATUS` + `_sleep` indirection as in `pipeline/domains/intel.py`, and honor `Retry-After`. Workday paginates by `offset` in pages of 20 and stops at `total`.

### Step 3: Resolver + discovery (`pipeline/jobs/resolve.py`, `discover.py`)

Ladder: `match_url` over known URLs → `probe` over slug candidates → HTML fingerprint (homepage → careers link → scan for ATS hosts) → `llm_resolve()` (Claude `web_search`/`web_fetch`, `max_uses=3`, structured output) → JSON-LD. Seed importers: `yc_oss.py` (hiring.json), `hn_hiring.py` (Algolia; extract ATS URLs from comment text), `inbox.py` (company names from job-alert `CollectedItem`s).

### Step 4: Poll + score + digest

- `pipeline/jobs/inventory.py::poll_all()` on `JOBS_JOB_ID` (template: `api/scheduler.py:64,101`). Upsert postings; mark closed after N missed polls.
- `pipeline/jobs/scoring.py`: batch new postings (`BATCH_SIZE=20`), pre-filter by title/location keywords before calling the model, use `make_client()` + a `JobFit` Pydantic schema.
- `pipeline/collectors/jobs.py::collect_jobs(since, until)`: emits `job_new` for postings with `fit_score ≥ threshold` whose `first_seen` falls in the window, plus `job_closed` for saved ones. Add the branch in `dispatch()`.

### Step 5: API + UI

`api/routers/jobs.py` (companies CRUD, boards with manual override, postings list/filter, rescan, profile). `web/pages/jobs.vue` with tabs Postings / Companies / Profile, plus `web/components/JobDetailDialog.vue`, `web/composables/useJobsApi.ts`, and a nav entry.

### Step 6 (phase 2): Apply playbooks

`pipeline/jobs/apply.py` with a playbook per `ats_kind` (a static step list plus per-tenant facts such as the Workday tenant login URL). `prepare_packet(posting)` calls Claude to produce tailored bullets, cover note and screening answers. `api/routers/jobs_apply.py`, `web/components/ApplyDialog.vue`. Application state changes emit digest todo items.

### Step 7 (phase 3): Apply assist — see §Apply Assist

### Configuration Changes

- `JOBS_REFRESH_CRON` (default daily) and a scoring threshold in `data/config.json` jobs section, following `domain_settings()`.
- No new secrets for the MVP. Optional `USAJOBS_API_KEY` / `ADZUNA_APP_ID` if those sources are added. The Claude web tools need web search enabled on the Anthropic org.
- Profile and resume live in the DB (gitignored `data/`), never in tracked files.

---

## Apply Assist

User requirement (2026-09-22): the system opens a browser, walks a checklist with the user, reads each page and enters all the information itself, and generates and attaches the cover letter. The user sits through the session and reviews. The user clicks the final submit.

### Where it runs

SignalSlate runs headless in Docker on a LAN server, and a visible browser needs a display. So the assist is a **local runner on the user's desktop**, in the same repo with a separate entry point (`python -m pipeline.jobs.assist`). It talks to the server over the existing authenticated API. Flow:

1. The user reviews the posting and packet in the SignalSlate UI and clicks **Apply**, which queues an assist session (`POST /api/jobs/applications/{id}/assist`).
2. The runner (a small desktop daemon polling `GET /api/jobs/assist/queue`) picks it up and launches Chrome.
3. The runner reports progress back (`PATCH …/assist/{session}`), so the UI and digest show where each application stands.

v1 could skip the daemon and use `python -m pipeline.jobs.assist <application_id>`; a custom `signalslate://` URL handler (xdg desktop entry) is a later nicety.

### Browser choice

Playwright `launch_persistent_context(user_data_dir=…/apply-profile, channel="chrome", headless=False)`:
- **Visible (headed)**: the user watches and takes over at any point.
- **Dedicated persistent profile**: Workday tenant logins, cookies and saved passwords survive between sessions. It stays separate from the user's daily profile.
- **No stealth plugins, no CAPTCHA solving**: human presence is the answer to bot checks. If a challenge appears, the session pauses and the overlay asks the user to solve it.
- **Python**: reuses the repo's stack, and `set_input_files()` handles resume and cover-letter uploads reliably, including hidden `<input type=file>`.
- **Later fallback**: a Chrome extension with a side panel (no automation fingerprint, the user's real session). Only worth it if CAPTCHA friction in the Playwright profile proves high. Costs a TypeScript/MV3 build and fiddlier file attachment.

### Page reading and filling (hybrid)

1. **Extract**: in-page JS walks the DOM, including same-origin and ATS iframes (Greenhouse embeds are iframes), and produces a compact field list: `{field_id, label, type, required, options, current_value, section}`. Labels come from `<label for>`, `aria-label`, `aria-labelledby` and nearby text.
2. **Deterministic mapping first**: a per-ATS adapter maps known fields (name, email, phone, LinkedIn, resume upload, standard Workday "My Information / My Experience" sections) straight from the profile. Workday exposes stable automation attributes (e.g. `data-automation-id`, to verify during build); Greenhouse, Lever and Ashby forms have stable field names.
3. **LLM for the rest**: remaining fields, plus the posting, profile and **answer bank**, go to Claude with structured output: `[{field_id, value, confidence, source: profile|answer_bank|generated|resume, needs_user: bool}]`. Free-text questions ("Why do you want to work here?") are drafted from the posting and company research, and shown for approval before filling.
4. **Fill**: Playwright `fill` / `select_option` / `check` / `set_input_files`, with human-ish pacing. After filling, re-read the fields to verify the values stuck (React-controlled inputs sometimes reject programmatic sets; fall back to `type()` keystrokes).
5. **Advance**: click the step's Next/Continue control. **Never click final submit.** The adapter identifies the submit control, a guard refuses any click on it, and the overlay tells the user "ready: review and click Submit".

### Checklist overlay

A small panel is injected into the page (shadow DOM, so page CSS can't touch it) showing:
- the playbook steps with ✓ / current / pending;
- for the current page, every field the system filled with value + source + confidence, low-confidence ones highlighted in the page;
- `needs_user` items: attestations ("I certify…"), anything the model could not answer, CAPTCHAs, email verification;
- buttons: **Fill page**, **Next step**, **Edit answer** (writes back to the answer bank), **Pause**.

Each ATS playbook defines its steps; for example, Workday: *sign in / create account → verify email → My Information → My Experience (resume upload + correct the parsed history) → Application Questions → Voluntary Disclosures → Self Identify → Review*. The generic playbook for unknown forms is one step per page.

### Cover letter and resume

- `prepare_packet()` (server side) generates the cover letter from resume + posting + company research, in the user's configured tone and template. The user can edit it in the UI before the session.
- Rendered to PDF on the server. Chromium `page.pdf()` in a headless context works if Chromium is added to the image; otherwise use an HTML→PDF library. This choice overlaps with README phase 4 (PDF render), so decide once for both.
- Resume variants: the profile holds one or more resume PDFs. The packet picks one (or the user does); v1 does not regenerate resumes.
- Files are stored per application, so the record keeps exactly what was sent.

### Data model additions

- `JobsProfile`, extended: contact info, links, work authorization / sponsorship, relocation, start date, salary expectation policy, EEO answers (default *decline to answer* unless the user sets otherwise), resume files, cover-letter tone and template.
- `AnswerBank(question_norm, question_raw, answer, source_application_id, updated_at)`: every answer the user approves or edits is saved and reused by similarity on later forms. After a few applications most questions fill with no model call.
- `JobApplication.assist_log` JSON: per step, the fields filled, values, sources and user overrides. It's the audit trail of what the system entered.

### Guardrails

- **Final submit, legal attestations and CAPTCHAs are always the user's.** Code-level guard, not just a prompt instruction.
- **Prompt injection**: page text is untrusted. The model only proposes values for enumerated `field_id`s. It never chooses actions, URLs or clicks; navigation comes from the playbook and the user. Hidden-text instructions on a job page therefore cannot trigger a submit or send data elsewhere.
- **PII**: resume and profile go to the Anthropic API for mapping and drafting (same trust decision as triage). The runner fetches the packet over LAN with API auth. Nothing personal is committed; test fixtures use synthetic profiles.
- **Credentials**: Workday per-tenant passwords go in the browser profile's password manager, or in the existing encrypted vault (`pipeline/crypto.py`) if the runner should fill them. Never plaintext in the DB.
- **Pacing**: one application at a time, user present. No batch or unattended mode.

### Effort

About 10–14 more files beyond the phase 2 estimate (runner entry point, extractor JS, overlay, 3–4 ATS form adapters starting with Greenhouse, Lever, Ashby and Workday, mapping prompt/schema, answer bank, assist API, tests with saved HTML form fixtures). This is an estimate by analogy to the adapter and dialog counts above; not measured.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Undocumented endpoints (Workday CXS, Workable widget, Rippling v2, BambooHR) change shape | Medium | Medium | One adapter per ATS, with contract tests on recorded fixtures; `last_error` per board shown in the UI; a failing board never fails the run |
| Slug probe false positive (another company owns `acme` on Greenhouse) | Medium | Medium | Verify hit via returned company name/website vs domain; store `confidence`; user can override |
| Workday coverage needs tenant + `wdN` + site, which slug probes cannot guess | High | Medium | HTML fingerprint + LLM research recover it from the careers link; manual paste of the careers URL as a first-class input |
| LLM cost creep (scoring every posting at large tenants with thousands of jobs) | Medium | Low | Keyword/location pre-filter before scoring; score only new postings; per-run cap, following triage batch sizing |
| Rate limiting / IP blocks from aggressive polling | Low | Medium | Daily cadence, serial per host, `Retry-After`, descriptive User-Agent |
| ToS/contract exposure if scope creeps into LinkedIn scraping or auto-submit | Low (by design) | High | The decision excludes both; the browser assist stops before submit and never touches CAPTCHAs |
| ATS form DOM changes break adapters mid-session | Medium | Low | LLM mapping handles unknown fields; user is present to finish by hand; save failing page HTML as a new fixture |
| Prompt injection via job-page text | Low | High | Model only proposes values for enumerated fields; code guard on submit; navigation is playbook-driven |
| Automation profile still draws CAPTCHAs | Medium | Low | User solves in-session; fall back to a Chrome-extension assist if friction is high |
| Resume/profile PII in a public repo | Low | High | Stored only in DB under gitignored `data/`; fixtures use synthetic profiles |

### Open Questions

- Whether Rippling's v2 list payload includes descriptions (probe showed list fields only) and what per-job endpoint supplies them. This matters for fit scoring quality.
- Whether `Feashliaa/job-board-aggregator` has a reusable, licensed company→board dataset worth an import feature.
- Adzuna/Jooble program status is unverified. Decide whether any aggregator beyond Remotive/HN earns an adapter.
- Where action items finally live: a local table only, or also pushed to the `tracker` (MS To Do / Todoist) sink that `config_store` anticipates but no code implements yet.
- Whether the existing triage stage should classify ATS confirmation/rejection emails so `JobApplication` status updates automatically (phase 2+).

---

## References

1. Greenhouse Job Board API — https://docs.greenhouse.io/job-board.html — public list; apply needs employer key
2. Greenhouse applications doc — https://github.com/grnhse/greenhouse-api-docs/blob/master/source/includes/job-board/_applications.md — Basic auth with employer key
3. Lever Postings API — https://github.com/lever/postings-api — public list; apply `?key=` employer-issued
4. Ashby Public Job Posting API — https://developers.ashbyhq.com/docs/public-job-posting-api
5. SmartRecruiters Posting API — https://developers.smartrecruiters.com/docs/posting-api
6. Live probes (2026-09-22): Greenhouse, Lever, Ashby, Workday CXS, SmartRecruiters, Workable, Rippling v2, BambooHR, Recruitee, Personio, yc-oss, HN Algolia, Remotive — URLs in frontmatter
7. yc-oss/api — https://github.com/yc-oss/api — static YC directory mirror
8. em-job-poller — https://github.com/plie/em-job-poller — curated slugs, description-excluded hashing
9. job-board-aggregator — https://github.com/Feashliaa/job-board-aggregator — claimed 20k-company dataset
10. Claude web search tool — https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool — $10/1k searches
11. Claude web fetch tool — https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool — token cost only, no JS
12. Greenhouse Invisible reCAPTCHA — https://support.greenhouse.io/hc/en-us/articles/115005448066-Invisible-reCAPTCHA
13. hiQ v. LinkedIn settlement — https://natlawreview.com/article/linkedin-s-data-scraping-battle-hiq-labs-ends-proposed-judgment
14. 9th Circuit post-Van Buren — https://www.proskauer.com/blog/taking-cue-from-the-supreme-courts-van-buren-decision-ninth-circuit-releases-new-opinion-holding-scraping-of-publicly-available-website-data-falls-outside-of-cfaa
15. Indeed publisher API shutdown — https://jobspipe.dev/blog/indeed-publisher-api
16. Sonara shutdown — https://blog.loopcv.pro/what-happened-to-sonara/
17. AI application flood — https://www.herohunt.ai/blog/surviving-the-ai-application-flood-2026-playbook/ (specific stats not found on page)
18. Application fraud detection — https://www.sardine.ai/blog/job-application-fraud-detection
19. Clearbit logo sunset — https://developers.hubspot.com/changelog/upcoming-sunset-of-clearbits-free-logo-api
20. `pipeline/collectors/__init__.py:79` — `dispatch()` routing
21. `pipeline/collectors/domains.py` — diff-only digest collector precedent
22. `api/scheduler.py:52,64,101` — domains job template
23. `pipeline/db.py:119,173` — `Domain`, `DomainPurchase` templates
24. `pipeline/domains/ideas.py` — `make_client()` reuse with own schema
25. `pipeline/config_store.py:18` — planned `tracker` sink
26. `tests/test_api_domain_buy.py:48-50` — test DB isolation pattern
