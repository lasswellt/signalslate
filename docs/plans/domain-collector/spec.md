---
status: active
priority: P1
created: 2026-09-21
ship: manual
---
# Domain collector: portfolio console, DNS/RDAP intel, name ideas, registrar purchase

## Goal
Give the owner one console for every domain they own, collected from Namecheap, GoDaddy and WordPress.com, and for any domain they are considering. It pulls all public information (DNS, mail posture, RDAP registration, subdomains and archived URLs), generates name ideas and checks availability, and can buy through the owner's Namecheap and GoDaddy accounts behind strict guardrails. Domain changes (expiry, NS/MX change, lock removed) reach the existing morning digest. Research: `docs/_research/2026-09-21_domain-collector.md`.

## Outcomes
- Every domain from connected Namecheap, GoDaddy and WordPress.com accounts, plus manually added or CSV-imported ones, appears in one Portfolio table with source, expiry, auto-renew, lock and nameserver provider; a daily job keeps it current. → T-002, T-003, T-005, T-006, T-007, T-008, T-013, T-014, T-018, T-020, T-021, T-022, T-023, T-028
- For any domain, owned or not, the console shows DNS records, mail posture (SPF/DMARC/DKIM/MTA-STS/BIMI), RDAP registration data and, on demand, CT-log subdomains and Wayback URLs, with per-part unsupported/unavailable states. → T-009, T-010, T-011, T-012, T-018, T-024, T-025, T-028
- The owner can generate name ideas (combinatorial, optionally Claude), see a private local availability signal, then an authoritative registrar check with price and premium flag. → T-016, T-019, T-024, T-027
- The owner can buy a domain through Namecheap or GoDaddy only when purchasing is enabled, from an unexpired server-side quote, under per-purchase and daily caps, after retyping the name; a repeat submit can never buy twice. → T-017, T-019, T-026, T-029
- The morning digest run includes a `domains` source whose items report snapshot changes and upcoming expiries in the run window. → T-012, T-015, T-029

## Out of scope
- DNS record editing at registrars (read-only DNS).
- Renewals, transfers, auto-renew toggling via API.
- urlscan.io / VirusTotal / Safe Browsing reputation (API keys + ToS).
- WHOIS port-43 fallback for non-RDAP ccTLDs (shown as "unsupported").
- GoDaddy v3 PAT endpoints (v1 sso-key only; auth kept behind one function).
- Domain valuation / aftermarket / backorders.
- Rendering domain alerts in the PDF (phases 3–4 of the digest are not built yet; items land in `collecteditem`).

## Assumptions
- `--autonomous`: no interview. "url search" means both availability search (Outcome 3) and URL/subdomain intel (Outcome 2).
- All four research phases (A inventory+intel, B digest, C ideas, D purchase) are in one plan; purchasing ships **disabled** (`DOMAINS_PURCHASE_ENABLED=false`) with defaults `DOMAINS_MAX_PRICE=25`, `DOMAINS_DAILY_CAP=50` (account currency) and premium names blocked (`DOMAINS_ALLOW_PREMIUM=false`).
- Registrar credentials are UI-only connections (not seeded from `.env`) and are read by adapters through `connections.get_secret()`; `materialize()` emits nothing for them.
- Registrant contact data for purchases is stored as an encrypted optional secret (`registrant_contact`, JSON) on each registrar connection.
- Multiple accounts per registrar are allowed (labelled like Slack/Gmail).
- WordPress.com uses the undocumented `/rest/v1.1/all-domains` with an OAuth `global`-scope token; CSV/manual import is the supported fallback. No refresh token: expiry shows as "sign in again".
- Name ideation reuses the existing Anthropic client (`pipeline/triage.py:375 make_client`, `pipeline/health.py:187 llm_settings`); no new LLM dependency.
- The owner's accounts may be below API thresholds (Namecheap 20 domains / $50; GoDaddy availability needs ≥50 domains or $20/mo). These surface as connection states, never crashes.
- Outbound HTTP uses `requests` (codebase convention); tests stub it at the module import site, as `tests/test_collectors.py:47` does.
- Namecheap `domains.check` batch size (~50) and GoDaddy price micro-units are `UNVERIFIED` in research; implementers confirm against vendor docs from a browser and keep them as named constants.

## Verification
- `bash /home/lasswellt/.claude/plugins/cache/blitz/blitz/3.8.1/scripts/tasks.sh verify domain-collector <id>` per task; `/blitz:check --scope plan domain-collector` before ship.
- Manual, before enabling purchase: run the full quote → purchase flow against Namecheap sandbox and GoDaddy OTE connections (`sandbox=true` / `environment=ote`). The sandbox availability universe differs from production, so passing is necessary but not sufficient.
- Manual: connect the real accounts, press Sync, and confirm the Portfolio count matches each registrar dashboard.
