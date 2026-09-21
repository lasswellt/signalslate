# Plan: domain-collector

## Architecture

A new `pipeline/domains/` package owns portfolio state in its own SQLModel tables (`Domain`, `DomainSnapshot`, `DomainQuote`, `DomainPurchase`). It is **not** a 24h collector, because `CollectedItem` is an event log (`pipeline/db.py:53`) and re-emitting the portfolio every run would break dedupe and watermark semantics. A second APScheduler job (`domains_refresh`) syncs registrar inventories and refreshes snapshots daily. A thin `domains` digest source then turns snapshot diffs in the run window into `domain_alert` items.

Layers:
- **Registrar adapters** (`pipeline/domains/registrars/{namecheap,godaddy,wordpress}.py`) behind one `Registrar` protocol with typed status errors (`NotEligible`, `IpNotWhitelisted`, `RateLimited`, `AuthFailed`, `Unsupported`). They provide the facts only a registrar knows: auto-renew, pricing, authoritative availability, purchase.
- **Registrar-neutral intel**: `dns.py` (dnspython plus mail posture), `rdap.py` (whoisit), `intel.py` (crt.sh plus Wayback, on demand). `snapshot.py` assembles and diffs them. This backbone works for every domain whatever the registrar or API tier.
- **Inventory** (`inventory.py`): registrar factory from connections, sync, manual/CSV, snapshot refresh.
- **Ideas** (`ideas.py`): combinatorial plus optional Claude, local DNS+RDAP prefilter (never a third-party search site).
- **Purchase** (`purchase.py`): server-side quote with TTL, guard chain (enabled, confirm name, premium, max price, daily cap, contact present), `DomainPurchase(pending)` row inserted before the registrar call with a unique `quote_id`, `unknown` on timeout reconciled by the next sync, never retried automatically.
- **API**: `api/routers/domains.py` (inventory and inspect) and `api/routers/domain_buy.py` (ideas, check, quotes, purchases). Both inherit the header+Origin CSRF model; the purchase guard is server-side because nothing else guards spending routes today.
- **UI**: `/domains` page (Portfolio, Watchlist, Ideas, Purchases) plus detail, ideas and purchase components; registrar kinds in the Connections dialog; WordPress.com sign-in via the existing OAuth dialog.

Tradeoffs:
- **Chosen**: in-app subsystem. Native digest integration, reuses the vault, connections, OAuth flows and redaction, and is the only option that covers purchase and ideas.
- **Rejected**: running Domain Locker alongside (monitoring only, Deno + Postgres second stack, digest needs webhook glue); forking DomainMOD (PHP/MySQL, no purchase).
- **Rejected**: modelling domains as `CollectedItem` events (breaks watermark/dedupe semantics).
- **Rejected**: materializing registrar keys into `_env()` (no collector reads them; `get_secret()` is fewer moving parts).

## File map

| Path | Change | Task |
|---|---|---|
| `requirements.txt` | + dnspython, defusedxml, whoisit | T-001 |
| `pipeline/db.py` | + 4 domain tables | T-002 |
| `pipeline/connections.py` | + namecheap/godaddy/wordpress `_Kind`s, label rules, contact validation | T-003 |
| `pipeline/domains/__init__.py` | new: `domain_settings`, `normalize_domain` | T-004 |
| `pipeline/domains/registrars/__init__.py` | new: protocol, dataclasses, typed errors | T-004 |
| `pipeline/domains/registrars/namecheap.py` | new: XML client | T-005 |
| `pipeline/domains/registrars/godaddy.py` | new: v1 client | T-006 |
| `pipeline/oauth_wordpress.py` | new: WP.com auth-code flow | T-007 |
| `pipeline/oauth_flows.py` | `PROVIDERS` + "wordpress" | T-007 |
| `pipeline/domains/registrars/wordpress.py` | new: all-domains best effort | T-008 |
| `pipeline/domains/dns.py` | new: records + mail posture | T-009 |
| `pipeline/domains/rdap.py` | new: RDAP via whoisit | T-010 |
| `pipeline/domains/intel.py` | new: crt.sh + Wayback | T-011 |
| `pipeline/domains/snapshot.py` | new: inspect + diff | T-012 |
| `pipeline/domains/inventory.py` | new: factory, sync, CSV, refresh | T-013 |
| `api/scheduler.py` | + `domains_refresh` job | T-014 |
| `pipeline/collectors/domains.py` | new: diff → items | T-015 |
| `pipeline/collectors/__init__.py` | dispatch `domains` | T-015 |
| `pipeline/health.py` | `known_sources` + `check_domains` | T-015 |
| `pipeline/domains/ideas.py` | new: generator, LLM, prefilter | T-016 |
| `pipeline/domains/purchase.py` | new: quote + guarded purchase | T-017 |
| `api/routers/domains.py` | new | T-018 |
| `api/routers/domain_buy.py` | new | T-019 |
| `api/main.py` | include 2 routers | T-018, T-019 |
| `api/routers/connections.py` | registrar create/update models + test dispatch | T-020 |
| `api/routers/oauth.py` | wordpress provider | T-021 |
| `web/composables/useApi.ts` | registrar kinds types | T-022 |
| `web/components/ConnectionDialog.vue` | registrar kind forms | T-022 |
| `web/components/OAuthDialog.vue` | wordpress provider | T-023 |
| `web/pages/connections.vue` | sign-in button for wordpress | T-023 |
| `web/composables/useDomainsApi.ts` | new | T-024 |
| `web/components/DomainDetailDialog.vue` | new | T-025 |
| `web/components/DomainPurchaseDialog.vue` | new | T-026 |
| `web/components/DomainIdeasPanel.vue` | new | T-027 |
| `web/pages/domains.vue` | new | T-028 |
| `web/app.vue` | + Domains tab | T-028 |
| `tests/test_domains_e2e.py` | new e2e | T-029 |
| `README.md`, `.env.example` | docs + keys | T-030 |

## Coverage

| Outcome | Schema | Connection/Auth | Adapter/Service | API | UI | Scheduler/Digest | Test |
|---|---|---|---|---|---|---|---|
| O1 Portfolio | ✓ T-002 | ✓ T-003, T-007, T-020, T-021 | ✓ T-005, T-006, T-008, T-013 | ✓ T-018 | ✓ T-022, T-023, T-028 | ✓ T-014 | ✓ per task + T-029 |
| O2 Intel | ✓ T-002 | — | ✓ T-009, T-010, T-011, T-012 | ✓ T-018 | ✓ T-024, T-025, T-028 | — | ✓ per task |
| O3 Ideas + availability | — | ✓ T-003 | ✓ T-016, T-005, T-006 | ✓ T-019 | ✓ T-024, T-027 | — | ✓ per task |
| O4 Guarded purchase | ✓ T-002 | ✓ T-003 (contact) | ✓ T-017 | ✓ T-019 | ✓ T-026 | — | ✓ T-017, T-029 |
| O5 Digest alerts | ✓ T-002 | — | ✓ T-012 | — | — | ✓ T-015 | ✓ T-015, T-029 |

No empty cells remain where an artifact is needed.

## Risks

- **API eligibility**: the owner's accounts may not meet the Namecheap (20 domains / $50) or GoDaddy availability (≥50 / $20/mo) thresholds. Mitigation: typed `NotEligible` states; RDAP, DNS and CSV keep O1 and O2 working.
- **Unverified vendor details**: Namecheap `domains.check` batch size, `domains.create` params, GoDaddy price units and payment-method selection (namecheap.com method pages return 403 to non-browser clients). T-005 and T-006 notes require named constants and confirmation from a browser at build time.
- **WordPress.com endpoint is undocumented** and may change or require cookie auth. T-008 parses defensively; CSV is the fallback.
- **GoDaddy policy/auth churn** (2024 restriction, 2026 PAT reports): one `_auth_headers()` function; typed 403.
- **Money**: guard chain plus unique intent row plus no auto-retry; ships disabled; manual sandbox/OTE run required before enabling (spec §Verification).
- **Namecheap IP whitelist** breaks when a home WAN IP changes: connection test reports the current egress IPv4.
- **T-015 touches 4 files** (soft-warn band): `health.py` must learn `domains` in both `known_sources()` and `check_all_configured()` because `runner.py:304-311` only collects healthy sources. Accepted; no other task in this plan touches `pipeline/health.py` or `pipeline/collectors/`.
- **Shared-file ordering**: `api/main.py` (T-018 → T-019) and `web/composables/useApi.ts` (T-022 only; domain fetchers live in the new `useDomainsApi.ts`) are serialized through `depends_on`.
- **Concurrent plan**: `management-ui` is still being checked on the same branch; T-003, T-020, T-021, T-022 and T-023 edit files it created. Build this plan after management-ui lands.
- SPIDR splits: none needed. No task exceeds 4 files and no horizontal-scope titles.

## Solutions consulted

- `docs/solutions/`: absent. No effect.
- `docs/plans/BACKLOG.md`: absent. No effect.

## Research

- `docs/_research/2026-09-21_domain-collector.md` (via `--from-research`; no agents spawned).
- Main-thread spot checks this session: `pipeline/connections.py:121,129,492,650`, `pipeline/health.py:516,526`, `pipeline/runner.py:304-311`, `pipeline/collectors/__init__.py:79`, `pipeline/oauth_flows.py:40`, `api/routers/oauth.py:68`, `api/scheduler.py:46`, `web/components/OAuthDialog.vue:174`, `web/pages/connections.vue:104`, `pipeline/triage.py:375`, `requirements.txt` (anthropic already pinned).
