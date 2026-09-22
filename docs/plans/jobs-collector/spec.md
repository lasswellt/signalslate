---
status: active
priority: P1
created: 2026-09-22
ship: manual
---
# Jobs collector: company research, ATS board polling, fit scoring, guided apply assist

## Goal
Find roles to apply to from employers' own applicant tracking systems (ATS), not only LinkedIn and job sites. Companies come from a watchlist, the YC hiring list, HN "Who is hiring" threads and job-alert emails already in the inbox. Each company is resolved to its ATS board (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Rippling, BambooHR, Recruitee, Personio, or schema.org JSON-LD). Boards are polled daily, and new postings are scored for fit against the owner's profile. Applying becomes a guided session: SignalSlate prepares a tailored cover letter and screening answers, opens a visible Chrome window on the owner's desktop, walks a per-ATS checklist, reads each page and fills it in, and attaches files. It stops for CAPTCHAs, attestations and the final Submit, which the owner clicks. Research: `docs/_research/2026-09-21_jobs-collector.md`.

## Outcomes
- Companies from a manual watchlist/CSV, the yc-oss hiring list, the latest HN "Who is hiring" thread and job-alert emails appear in one Companies list. Each is resolved to an ATS board (kind, board id, how it was resolved, confidence) or shown as unresolved, and the owner can override the board. → T-002, T-003, T-004, T-005, T-006, T-007, T-008, T-009, T-010, T-011, T-012, T-013, T-014, T-015, T-020, T-031, T-035
- A daily job polls every resolved board. Postings are tracked with first/last seen and closed after N consecutive misses. A failing board records `last_error` and never fails the run. → T-004, T-005, T-006, T-007, T-008, T-009, T-010, T-015, T-018
- New postings are pre-filtered and scored 0–100 against the profile with a reason. The Postings list filters by score, status, remote and text, and high-fit new postings, closures of applied postings and application updates reach the morning digest as a `jobs` source. → T-016, T-017, T-019, T-020, T-031, T-032, T-036
- For any posting, the owner creates an application and gets a generated cover-letter PDF and drafted screening answers from the profile and answer bank. The owner edits them, and the application's status is tracked from saved to submitted to outcome. → T-016, T-021, T-022, T-023, T-031, T-033, T-034, T-036
- "Start assist" opens a visible Chrome on the owner's desktop with a checklist panel. The runner fills fields and uploads, advances through pages, and pauses for CAPTCHA, attestation and unanswered fields. It never clicks the final submit and never marks an application submitted. Approved answers are saved to the answer bank, and progress shows in the UI. → T-001, T-024, T-025, T-026, T-027, T-028, T-029, T-030, T-033, T-037

## Out of scope
- LinkedIn, Indeed or Glassdoor scraping or login automation. Job-alert *emails* already in the inbox are used instead.
- Unattended or batch submission, CAPTCHA solving, stealth or anti-detection browser plugins, and automatic account creation without the owner present.
- Keyed aggregator sources (USAJobs, Adzuna, Jooble) and Remotive. Optional per research; left for a later plan.
- Resume regeneration or tailoring into a new resume file. The packet picks one of the owner's uploaded resume PDFs.
- Chrome-extension assist (research fallback, only if CAPTCHA friction in the Playwright profile proves high).
- Pushing action items to MS To Do / Todoist (the `tracker` sink is not built anywhere yet).
- Email-driven application status inference (confirmation/rejection emails). Listed as a research open question.
- Rendering jobs items in the digest PDF (phases 3–4 of the digest are not built; items land in `collecteditem`).

## Assumptions
- `--autonomous`: no interview. All research phases (discover/poll/score, apply packet, guided assist) are in one plan. Task order ships the collector first, so T-001–T-020 are useful without the assist.
- Assist launch: a desktop runner, `python -m pipeline.jobs.assist --watch`, polls the server queue. `python -m pipeline.jobs.assist <application_id>` runs a single session. No custom URL-scheme handler.
- The runner never holds the Anthropic key. Page-to-value mapping runs server-side behind `POST /jobs/assist/sessions/{session_id}/propose`, and the runner sends only the extracted field list.
- Cover-letter PDFs are rendered server-side with `fpdf2` (pure Python, no system libraries in `python:3.12-slim`). This does not decide the digest PDF renderer (README phase 4).
- Playwright lives in `requirements-assist.txt`, which is installed on the desktop and in the dev venv (tests use headless Chromium on synthetic HTML fixtures). The server Docker image does not get it.
- Workday per-tenant passwords are kept by the assist browser profile's own password manager (persistent profile at `~/.local/share/signalslate/apply-profile`), not in SignalSlate's vault.
- The API has no login (`api/security.py`). The runner authenticates like the web UI (`X-Requested-With: signalslate`, host in `ALLOWED_HOSTS`). The API is LAN-only, as today.
- Resume upload is a JSON body with base64 content (the API accepts JSON bodies only), PDF only, capped at 5 MB, stored under gitignored `data/`.
- EEO/voluntary-disclosure answers default to "decline to answer" unless the owner sets them in the profile.
- Undocumented endpoints (Workday CXS, Workable widget, Rippling v2, BambooHR, Recruitee) are used as verified live on 2026-09-22. Adapters are defensive, and breakage shows per board.
- Outbound HTTP uses `requests` with `_sleep` indirection and retryable-status handling, as in `pipeline/domains/intel.py`. Tests stub HTTP at the module import site and use a fake Anthropic client. Neither is a mock of `src/`.
- LLM spend is capped by `JOBS_MAX_LLM_RESOLVES_PER_RUN` and `JOBS_MAX_SCORE_PER_RUN`, with a keyword/location pre-filter before scoring.

## Verification
- `bash /home/lasswellt/.claude/plugins/cache/blitz/blitz/3.8.1/scripts/tasks.sh verify jobs-collector <id>` per task; `/blitz:check --scope plan jobs-collector` before ship.
- Manual: add 5 real companies (at least one each on Greenhouse, Lever, Ashby and Workday), run the jobs refresh, and confirm the resolved boards and posting counts match each careers page.
- Manual: on the desktop, `pip install -r requirements-assist.txt && python -m playwright install chromium`, run `python -m pipeline.jobs.assist --watch`, start an assist on a real Greenhouse posting, and confirm fields fill, the cover letter attaches and the session stops at Submit. Close the tab without submitting.
