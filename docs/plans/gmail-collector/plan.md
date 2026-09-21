# Plan: gmail-collector

Class: architectural. Research: `docs/_research/2026-09-20_gmail-collector-and-agents.md`
(`--from-research`, no agents spawned).

## Architecture

**Collector.** A `gmail_<label>` source, one per `GMAIL_<LABEL>_REFRESH_TOKEN` in `.env`, dispatched
through the existing `dispatch()` seam and persisted by the existing runner (cursor, overlap,
backfill cap, dedupe on message id). No runner changes beyond de-duplicating `_active()`.

- **Auth:** per-user Desktop OAuth client, loopback + PKCE, scope `gmail.readonly` only. Refresh
  via a plain POST in `health.py` (mirrors `zoom_token_response`). `invalid_grant` is a hard error
  with a re-auth message; transient errors back off.
- **Read path:** `messages.list` with `q=after:<epoch> before:<epoch>`, `maxResults=500`, drain all
  pages, then sequential `messages.get?format=full`. Window re-checked on `internalDate`.
- **Payload:** flattened in the collector (decoded body, selected headers, labels, thread id,
  attachment metadata), a deliberate small deviation from "store raw" because Gmail's raw form is a
  nested base64 MIME tree. Hidden HTML is dropped at this layer.

**Agents (Phase 3 slice).** A workflow, not an agent loop: `NormalizedItem` adapter (pure) →
alias assignment → batched map calls with `messages.parse()` and a Pydantic schema → validation →
stub fallback. No tools, JSON-encoded untrusted content, model text sanitized. Reduce is later.

**Rejected:**
- `history.list`/`watch`: persisted cursor plus a full-sync fallback, or Pub/Sub, for a once-daily
  pull that a timestamp read already self-heals.
- Gmail HTTP batch: same envelope-hides-429 failure mode as Graph `$batch`, identical quota cost.
- IMAP + app password: viable fallback, but broader grant, and Google steers away from it; kept as
  the documented plan B, not built.
- Agent SDK / Managed Agents: no tool loop needed, and Managed Agents holds mail server-side and
  is not ZDR-eligible.
- Storing the token in a `tokens/` file: Google returns no rotated refresh token, so a file buys
  nothing over `.env`.

## File map

| Path | Change | Task |
|---|---|---|
| `requirements.txt` | pin `anthropic==<resolved>` (T-001 only verifies existing pins) | T-001, T-011 |
| `pipeline/health.py` | `GMAIL_` env prefix, `gmail_accounts()`, `gmail_token_response()`, `check_gmail()`, `known_sources()`/`check_all_configured()` branches, `llm_settings()` | T-002, T-011 |
| `tests/conftest.py` | `GMAIL_` prefix, `ANTHROPIC_API_KEY` and model key in `CONFIG_KEYS` | T-002, T-011 |
| `tests/test_health.py` | Gmail health cases | T-002 |
| `pipeline/runner.py` | `_active()` uses `known_sources()`; drop unused imports | T-003 |
| `pipeline/db.py` | source-id comments; `items_for_source_since()` | T-003, T-014 |
| `tests/test_runner.py` | gmail source appears in `_active()` | T-003 |
| `pipeline/collectors/gmail.py` (new) | transport, MIME flattening, `collect_gmail` | T-004, T-005, T-006 |
| `pipeline/collectors/__init__.py` | `gmail_` branch in `dispatch()` | T-006 |
| `tests/test_collectors.py` | `test_gmail_*` | T-004, T-005, T-006 |
| `tests/fixtures/gmail_message_multipart.json` (new) | synthetic Gmail API message | T-005 |
| `auth/gmail_bootstrap.py` (new) | loopback + PKCE sign-in | T-007 |
| `tests/test_gmail_bootstrap.py` (new) | pure-helper tests | T-007 |
| `README.md`, `.env.example` | Gmail setup, env entries, Anthropic key | T-008, T-011 |
| `pipeline/normalize.py` (new) | `NormalizedItem`, `normalize_gmail` | T-009 |
| `tests/test_normalize.py` (new) | adapter over real `flatten_message` output | T-009 |
| `pipeline/triage.py` (new) | schema, aliases, validation, stubs, request builder, `triage_items` | T-010, T-012 |
| `tests/test_triage.py`, `tests/test_triage_client.py` (new) | pure logic; SDK-signature contract + fake-client behavior | T-010, T-012 |
| `tests/test_triage_injection.py`, `tests/fixtures/injection_emails.json` (new) | injection regression suite | T-013 |
| `pipeline/triage_cli.py` (new), `tests/test_triage_cli.py` (new) | `--dry` inspection over collected items | T-014 |

## Coverage

| Outcome | Auth/health | Transport | Payload | Wiring | Docs | Adapter | Triage | Tests |
|---|---|---|---|---|---|---|---|---|
| Declared as `gmail_<label>`, health explains failure | ✓ T-002 | — | — | ✓ T-003, T-006 | — | — | — | ✓ T-002, T-003 |
| Bootstrap prints a valid token | ✓ T-007 | — | — | — | ✓ T-008 | — | — | ✓ T-007 |
| Dry run and runs collect last 24h | — | ✓ T-004 | ✓ T-005 | ✓ T-006 | — | — | — | ✓ T-004..T-006 |
| Cannot under-collect or blow quota | — | ✓ T-004 | — | — | — | — | — | ✓ T-004 |
| Items normalize and triage with provenance | — | — | — | — | — | ✓ T-009 | ✓ T-010, T-012, T-014 | ✓ T-009, T-010, T-012 |
| Hostile email cannot steer the summarizer | — | — | ✓ T-005 | — | — | — | ✓ T-012 | ✓ T-013 |
| Setup documented, public-safe | — | — | — | — | ✓ T-008, T-011 | — | — | — |

No gaps. Task order for parallel work: T-001 → T-002 → {T-003, T-004, T-007}; T-004 → T-005 → T-006;
T-005 → T-009 → T-010; T-006+T-007 → T-008; T-002+T-008 → T-011; T-010+T-011 → T-012 →
{T-013, T-014 (also needs T-003)}. Tasks that share a file are ordered by dependency (checked).

## Risks

- **No dev environment on this host.** No `venv/`, no pytest, Python 3.14. A pinned dependency may
  lack a wheel for that interpreter. T-001 exists to find out first and stops the plan on a red
  baseline.
- **Token lifetime is the real risk, and the code cannot test it.** The 7-day Testing rule is
  documented; whether production-unverified tokens outlive day 8 is only anecdotally confirmed.
  Manual Step 0 and the day-8 check in `spec.md` gate confidence, not just code.
- **Phase 3 slice designed before live data.** T-009/T-010 fix a schema and truncation policy before
  seeing a real Gmail payload. The dry-run checkpoint before T-009 is recommended; treat T-009 to
  T-012 as revisable if the dry run surprises.
- **SDK kwarg drift.** The structured-output parameter moved (`output_format` to
  `output_config.format` in docs) and the SDK helper name is not sourced. T-012 must read the
  installed SDK signature, and its contract test compares against it so a stub client cannot mask a
  wrong name.
- **Haiku 4.5 retirement** is "not sooner than 2026-10-15" with 60 days' notice, landing during this
  work. Model id is env-configurable (T-011); re-check the deprecations page when starting T-012.
- **`BODY_CAP`, `PAGE` sizes, batch size ~20 are guesses.** Marked as such in task notes; re-tune from
  real data.
- **Unverified API details** the tests can pin only as assumptions: `after:`/`before:` boundary
  inclusivity, list ordering, draft exclusion, `snippet` under `format=metadata`. The collector
  tolerates them (overlap, client-side window, id dedupe); the dry run settles them.
- **Password change on the Google account kills the token silently**, and repeated bootstrap runs can
  evict a token already deployed (100-per-client cap). Documented in T-007's warning and T-008.
- **Third-party email leaves the LAN** to the LLM vendor on any live triage run; retention terms for
  the chosen API tier were not checked in the research.
- **Public repo hygiene:** fixtures are synthetic and T-008 has a grep guard; no client id may ever
  be committed.

## Solutions consulted

None: `docs/solutions/` and `docs/plans/BACKLOG.md` do not exist in this repo.

## Research

- `docs/_research/2026-09-20_gmail-collector-and-agents.md` (citation check PASS; four minor
  attribution fixes applied)
- Earlier context: `docs/_research/2026-09-13_phase2-collectors.md`,
  `docs/_research/2026-09-13_auth-approach.md`
