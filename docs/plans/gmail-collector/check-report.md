---
result: FAIL
ts: 2026-09-21T04:05:14Z
ref: 1a3dfe9df2a2d42edae809dcc1e78a8d863aff9d
scope: plan
plan: gmail-collector
---

# Check — gmail-collector

**Result:** FAIL · **Base:** d382879 (main) · **Files:** 30 · **LOC:** +7074/−21

## Summary

All 16 built tasks re-verify, the suite is 500 passed (120 on main), and the critic returned LGTM with 16/16 held-out checks passing. FAIL is on two reproduced P1 findings, one root cause: nothing isolates a hostile message, so a bad charset or deep MIME tree aborts the whole collection and, via the runner's 3-strike watermark advance, loses the window. Both are tracked as T-017 (plus P2s as T-018, T-019). A human still owns the live-Gmail dry run, the day-8 token check, and the 54 pre-existing pyright errors.

## Gates

| Gate | Before fix | After fix | Status |
|---|---|---|---|
| typecheck (pyright, `python-pyright` lane) | 86 errors (venv invisible) | 54 errors, identical to main's 54; 0 introduced | FAIL (pre-existing debt only) |
| lint | — | — | SKIPPED (no python lint row in the toolchain) |
| tests (full) | 500/500 passed · escaped 0 | 500/500 passed | PASS |
| build | — | — | SKIPPED (no build lane; `docker build` not run) |

Pyright note: the lane could not see `venv/`, so every third-party import was "unresolved" (33) and SQLModel typing cascaded. `pyrightconfig.json` fixes the cause. Of the 54 that remain, none is new on this branch. They are SQLModel `.desc()`/Optional patterns in `db.py`/`runner.py` and Optional narrowing in older tests. The tests gate ran `pytest` directly because the toolchain has no python test row.

## Tasks (plan scope)

| Task | passes | last_verify.ok | failed | tail |
|---|---|---|---|---|
| T-001 … T-016 (16 tasks) | true | true | — | — |
| T-017, T-018, T-019 (origin: check) | false | — | not yet built | new this run |

## Held-out checks (critic-authored, plan scope)

16/16 `ok: true`, one per task, each probing from a different angle than the task's own `verify[]`. Highlights:

| Task | ok | What it proved beyond verify[] |
|---|---|---|
| T-001 | true | interpreter is 3.12; `pip check` clean; all 9 pins equal installed versions |
| T-002 | true | `invalid_grant` → error naming the bootstrap script and the Testing-mode cause; refresh token not leaked into the detail |
| T-003 | true | `_active` really delegates to `known_sources()` (a fabricated source flows through) |
| T-004 | true | epoch window identical across 4 container timezones and equal to `calendar.timegm`; page drain; `MAX_PAGES` raises |
| T-005 | true | fresh nested multipart: base64url, text/plain preferred, attachment metadata, hidden HTML dropped |
| T-006 | true | end-to-end `dispatch('gmail_work')` happy path; neither token appears in payload or detail |
| T-007 | true | challenge is real S256 of the verifier; scope is `gmail.readonly` only; state mismatch raises |
| T-008 | true | 0 personal/credential hits across all 30 changed files; `.env.example` keys ship empty |
| T-009 | true | SENT → outbound, List-Unsubscribe → bulk, snippet unescaped, permalink stays `None` |
| T-010 | true | `extra=forbid`, hallucinated alias dropped, duplicate keeps first, missing reported |
| T-011 | true | `anthropic==1.7.0` pinned and installed; blank key → `None`; missing key → `TriageConfigError` |
| T-012 | true | `output_format` is a real `Messages.parse` parameter; dead client → all items stubbed, none lost |
| T-013 | true | a hostile email absent from the fixture: breakout stays inside the JSON string, system prompt byte-identical |
| T-014 | true | `--dry` opens no socket; DB byte-identical after a run |
| T-015 | true | model receives `from:`/`to:`/`cc:`-prefixed participants |
| T-016 | true | 8 scheme forms stripped (7 not in the existing tests); 5 lookalikes preserved |

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| `after:`/`before:` epoch boundary vs `internalDate`; list ordering | real account | open: manual dry run (`python -m pipeline.collect gmail_<label> --raw --hours 2`); code overlaps 30 min and re-checks `internalDate`, so no drop found statically |
| Are Subject/From RFC 2047-decoded in `payload.headers`? | real `format=full` payload | open: dry run; the adapter assumes decoded |
| Real max MIME nesting depth from Gmail | empirical | open; T-017 makes the collector robust to any depth regardless |
| Does the (future) digest renderer HTML-escape one_line/action_text/people? `sanitize_text` leaves an unclosed `<a href=x` in place | renderer (Phase 4) | open: carried to the render phase |
| Oversized subject/recipients pushing a 20-item batch past model context | fixture | open (P3): the batch would be stubbed, not lost |
| Web UI toggling of a new `gmail_<label>` | running app | static trace only: `config_store` default-on, `config.vue` iterates keys dynamically, `_active` delegates, `dispatch` branch exists |
| README claims about Google policy (100 tokens/client, unverified-app click-through, 100-user exemption) | human check vs current Google docs | open: taken from the research doc |
| `test_triage.py`, `test_triage_cli.py`, T-016 sanitizer tests: could they pass on broken behavior? | mutation run | open: the patterns lens did not reach them; the critic mutation-tested the injection suite |

## Findings

### Critical (blocks)

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | pipeline/collectors/gmail.py:`_decode_part` | 🔴 `charset=undefined` and `charset=idna` raise `UnicodeError` out of `flatten_message` (only `LookupError` is caught). Reproduced. Catch `(LookupError, UnicodeError)`, fall back to utf-8/replace. → T-017 | survey:security | 0.9 |
| 2 | pipeline/collectors/gmail.py:`collect_gmail` | 🔴 No per-message guard around `flatten_message`: one bad message (or a 2000-deep MIME tree → `RecursionError`) discards every fetched message and returns no result; the runner then counts a hard failure and after `MAX_STUCK_RUNS=3` advances the watermark past the window. Wrap per message, record a failure, return `partial`. → T-017 | survey:backend | 0.8 |

### Major

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 3 | pipeline/collectors/gmail.py:`_TextExtractor.handle_endtag` | 🟡 Quadratic on unmatched end tags: 2.16 s at 30 KB, ~9 s at 140 KB, minutes at ~1 MB; the HTML path is reached whenever text/plain is empty (sender-controlled) and input is unbounded before parsing. Cap input and make the check O(1). Reproduced. → T-018 | survey:security | 0.85 |
| 4 | auth/gmail_bootstrap.py:`wait_for_code` | 🟡 No per-connection timeout: an idle local connection blocks the accept loop 4 s past a 1 s deadline (reproduced), so the flow can hang. Also no test pins the bind to 127.0.0.1. → T-019 | survey:security | 0.8 |

### Minor

LGTM. Below the 0.8 gate, not surfaced as findings; see Later.

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 5 | pipeline/collectors/gmail.py:64 | ❓ `to_epoch_seconds` overwrites rather than converts a tz-aware datetime (a −07:00 value shifts 7 h). Latent: documented contract is naive UTC and the only caller passes naive UTC. | critic | 0.9 |

## Auto-fix (`--fix`)

| Category | Found | Fixed | Remaining | Skipped |
|---|---|---|---|---|
| Imports / exports | 1 root cause (33 unresolved imports: pyright blind to venv) | 1 (`pyrightconfig.json`) | 0 | 0 |
| Type errors | 4 new on this branch (+54 pre-existing) | 4 | 54 pre-existing | 54 (not this plan's code) |
| Lint | — | — | — | no lane |
| Framework (F5/V3/P2) | — | — | — | not a Vue/Firestore repo |
| Naming / return types | 0 | 0 | 0 | 0 |
| Unused | 3 obsolete `# pyright: ignore` comments | 3 | 0 | 0 |

Not auto-fixed by policy (security and logic findings): items 1-4 became tasks T-017…T-019 instead.

Commits: `208a9f0 fix(check): imports — point pyright at venv/, drop obsolete ignores` · `69d36ca fix(check): type errors in gmail.py — resp possibly unbound in _call` · `1a3dfe9 fix(check): type errors in tests — Optional narrowing and the SDK request stand-in`. No assertion was changed; two tests gained a narrowing statement.

Also: T-013's plan note quoted a test phrase that the startup validator flags as an injection marker; it was reworded through `tasks.sh` (the phrase itself stays in the test fixtures, which are not scanned).

## Ratchet

`docs/sweeps/ratchet.json` does not exist and its detectors are TypeScript-oriented (`tsc`, `as any`, vitest test counts, `src/`), so they would record meaningless zeros here; none was created. Python equivalents, informational:

| Metric | Main | Current | Δ |
|---|---|---|---|
| type_errors (pyright, venv visible) | 54 | 54 | 0 |
| test_count (pytest collect) | 120 | 500 | +380 |
| TODO/FIXME in source | 0 | 0 | 0 |
| mocks in non-test source | 0 | 0 | 0 |

The skill's absolute type-error floor of 0 makes the pre-existing 54 a mechanical FAIL on their own; they are unrelated to this branch.

## Critic

`--mode reject` verdict: LGTM · mode: in-Claude (opus, fresh context, resumed once after a turn-limit stop) · findings: 1 (P3, the latent tz-aware note above)

## Security posture

registry-validate: ok · startup-validate --strict: non-zero, no injection (one SCHEMA finding: hook-owned `.cc-sessions/typecheck-baseline.json`, `schema_version: 2`, lacks fields the validator expects, a plugin-side mismatch; the earlier T-013 injection-marker false positive was cleared) · pre-trust execution: none in project content (the one `eval` grep hit is a variable-indirection line inside the plugin's own `critic-external.sh`)

## Automation coverage

Deterministic gates: 3/5 (typecheck fails on pre-existing debt only; tests pass; lint and build have no python lane; ratchet not applicable; registry rows 15 of 19 ran, the other 4 being operational or folded into others; critic LGTM) · e2e: skipped_unavailable
Not auto-verified (human owns): behavior against real Gmail, the Anthropic structured-output round trip on real data, triage quality, architectural fit, business intent, and anything in the not-yet-built render phase.
**Recommendation:** needs-human-review

## Before merge

1. Build T-017 (per-message isolation + charset), then T-018 and T-019: `/blitz:build gmail-collector`.
2. Run the live dry run: `python -m pipeline.collect gmail_<label> --raw --hours 2 --limit 3`. It settles boundary, ordering, header-decoding and payload-size questions and lets you re-tune `BODY_CAP`.
3. Do the day-8 token check (README) once a real account is authorized.
4. Decide whether the 54 pre-existing pyright errors get their own cleanup task (SQLModel `col()` wrappers in `db.py`, Optional handling in `runner.py`, narrowing asserts in older tests).

## Later

1. `strip_quoted_reply` drops everything after the attribution line, so a bottom-posted reply loses all its text and triage gets an empty body (reproduced by the backend lens, conf 0.65). Conservative fix in line with the module's own design: drop only the attribution and the `>` block, keep the rest.
2. A model `one_line` that sanitizes to empty is kept as a valid record (blank digest line); apply the `NO_SUBJECT`-style guard in `validate_batch`.
3. `sanitize_text` is quadratic on huge subjects (`'['*100000` took 19 s) and `stub_record` feeds it uncapped input; cap before sanitizing. Bidi override and zero-width characters survive it.
4. `_call` sleeps ~63 s per retry-exhausted id, so a sustained outage stalls the run; add a consecutive-failure abort. `requests.RequestException` is raised without retry.
5. Test strength (P3): the list-level epoch test does not pin a non-UTC `TZ`; the `MAX_PAGES` test never counts pages requested; nothing pins the loopback bind (folded into T-019).
6. Docs: README says every bootstrap run mints a token, but a healthy configured token short-circuits without minting; `triage_cli` is not in the README and needs a persisted run first.
7. `to_epoch_seconds` should `astimezone(utc)` when handed an aware datetime.
8. Recorded, unfixable in this plan: white-on-white / font-size:0 text still reaches the model (a test pins it deliberately).
9. Re-record the hook's typecheck baseline (76 → 54) and fix its schema mismatch with `startup-validate`; add a python test lane to the toolchain so the tests gate stops being a manual run.
