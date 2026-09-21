---
result: FAIL
ts: 2026-09-21T05:08:29Z
ref: 5f85deb31f1353d8de2e226fa56c4fff914ba458
scope: plan
plan: gmail-collector
---

# Check — gmail-collector (re-check on main, after PR #1)

**Result:** FAIL · **Base:** d382879 (pre-branch main) · **Files:** 29 · **LOC:** +7820

## Summary

The four issues from the first check (2×P1, 2×P2) are fixed and hold up: I re-measured each and three lenses found no regression in them. This run adds one new P1 that the first pass missed, found by the backend lens and reproduced end to end here: a sender-chosen charset (`unicode_escape`, `raw_unicode_escape`, `utf-7`) yields a lone surrogate in `bodyText`; the collector stores it fine, but `triage_items` with the real SDK client raises `UnicodeEncodeError`, breaking its never-raises contract and stopping the whole map stage. Also open: a self-closing hidden-tag bypass of the hidden-content guarantee, and some test-strength gaps. This run changed no code (no `--fix`). A human still owns the live-Gmail dry run, the day-8 token check, and the 54 pre-existing pyright errors.

## Gates

| Gate | Before fix | After fix | Status |
|---|---|---|---|
| typecheck (pyright, venv visible) | 54 errors | n/a (no `--fix`) | FAIL: 54 pre-existing, identical to pre-branch main; 0 introduced |
| lint | — | — | SKIPPED (no python lint row) |
| tests (full) | 525/525 passed · escaped 0 | n/a | PASS |
| build | — | — | SKIPPED (no build lane; `docker build` not run) |

## Tasks (plan scope)

| Task | passes | last_verify.ok | failed | tail |
|---|---|---|---|---|
| T-001 … T-019 (19 tasks) | true | true | — | — |

## Held-out checks (critic-authored, plan scope)

22 entries: 19 tasks plus 3 extras. 21 `ok: true`, 1 `ok: false`. The 16 checks for T-001…T-016 were re-runs of the previous run's checks (regression pass); T-017…T-019 and the extras are new.

| Task | ok | What it proved |
|---|---|---|
| T-001 … T-016 | true | regression: unchanged behavior (interpreter 3.12, pins match, `invalid_grant` message and no token leak, `_active` delegation, epoch window, MIME flatten, end-to-end dispatch, S256 PKCE, hygiene scan, adapter, validate_batch, settings, real `Messages.parse` signature, injection request shape, `--dry` opens no socket, participant roles, sanitizer schemes) |
| T-017 | true | [good, 4000-deep MIME poison, charset=undefined+idna, good] → `partial`, three messages kept, only the deep one failed; failure text is class-name-only |
| T-018 | true | 1.05 MB hostile HTML in 0.28 s (was minutes), `bodyTruncated=True`, hidden text still stripped |
| T-019 | true | real callback served in 0.05 s behind an idle connection; idle-only run times out at 1.02 s |
| extra: bodyTruncated | true | both causes (BODY_CAP and MAX_HTML_CHARS) set the flag |
| extra: loopback bind | true | bound to 127.0.0.1; the host's own non-loopback address is refused |
| extra: triage_items never-raises | **false** | ValueError, TypeError, KeyError, RuntimeError, RecursionError and UnicodeEncodeError all escape (only `anthropic.APIError` and `ValidationError` are caught at `pipeline/triage.py:402`); this is the mechanism of finding 1 |

Rule note: the skill treats a failing held-out check as a REJECT. The critic returned `LGTM` overall because that failure reproduces already-recorded finding 1 and is not a new defect. The run is FAIL either way, so the distinction does not change the result.

## Cannot verify (survey)

| What | Needs | Resolution |
|---|---|---|
| `after:`/`before:` boundary vs `internalDate`; list ordering; RFC 2047 header decoding; real max MIME depth; Gmail header-size limits | real account | open: live dry run (`python -m pipeline.collect gmail_<label> --raw --hours 2`) |
| Can Gmail's JSON carry a lone surrogate in a header or snippet (same crash without the charset trick)? | live `messages.get` of a CESU-8 / bad encoded-word subject | open; finding 1's fix should cover it at the request boundary regardless |
| Does the (unbuilt) digest renderer escape, entity-decode, markdown-render or autolink triage text? | renderer | open: carried to Phase 4; decides whether the sanitizer survivors below are exploitable |
| Is the outcome-lock race in `wait_for_code` covered? | stress test | open: only first-wins logic was mutated (killed) |

## Findings

### Critical (blocks)

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 1 | pipeline/collectors/gmail.py:`_decode_part` → pipeline/triage.py:`triage_items` | 🔴 A sender-chosen charset (`unicode_escape`, `raw_unicode_escape`, `utf-7`) decodes to a lone surrogate in `bodyText`. Persistence is fine (`json.dumps` escapes it), but building and sending the triage request raises `UnicodeEncodeError` out of `triage_items` (real SDK client, reproduced): one email stops the whole map stage, and `triage_cli` crashes printing it. Fix at both ends: restrict charsets to real text codecs (or scrub surrogates after decode), and let `triage_items` degrade any call failure to stubs instead of only `APIError`/`ValidationError`. | survey:backend + critic held-out | 0.95 |

### Major

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 2 | pipeline/collectors/gmail.py:`_TextExtractor` | 🟡 Self-closing hidden tags leak their text: `<div style="display:none"/>SECRET</div>` extracts `SECRET` (the parser closes the region at once; browsers ignore `/>` on non-void tags and keep it hidden). Reproduced for `div`, the `hidden` attribute, `aria-hidden` and `script`; the properly closed form hides correctly. Rated P3 by the lens; listed Major because it bypasses the module's stated guarantee, distinct from the accepted white-on-white limit. Add `handle_startendtag` that treats non-void self-closing tags as opening. | survey:security, critic | 0.85 |
| 3 | pipeline/triage.py:265 | 🟡 Mutant `person = sanitize_text(sender_label, PERSON_MAX)` → `person = sender_label` survives 212 tests: nothing feeds a hostile From display name to `stub_record`/`triage_items`. A test gap on a security-relevant path, not a live defect. | survey:patterns (mutation) | 0.95 |

### Minor

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 4 | auth/gmail_bootstrap.py:`exchange_code` | 🔵 A non-200 response whose JSON body is an array/scalar raises `AttributeError` (not `TokenExchangeError`); `main()` catches only `BootstrapError`, so the user sees a raw traceback. No secret is printed. Reproduced. | survey:security | 0.9 |
| 5 | tests/test_triage_cli.py | 🔵 `--dry` writing a DB row goes undetected; only the live path asserts "writes nothing". | survey:patterns (mutation) | 0.9 |
| 6 | auth/gmail_bootstrap.py:`log_message` | 🔵 Nothing pins the deliberate suppression of the request line (it carries `?code=`); printing it survives all tests. | survey:patterns (mutation) | 0.9 |
| 7 | tests/test_collectors.py:958 | 🔵 Dropping only `head` or only `title` from `_SKIP_TAGS` survives: the single test puts `<title>` inside `<head>`, so each masks the other. | survey:patterns (mutation) | 0.9 |

Mutation coverage: 84 mutants run, 76 killed (90%). The killed set includes every T-017 (guard, charset, RecursionError), T-018 (cap, flag, counter, O(n) scan) and T-019 (timeout, deadline, first-wins, 0.0.0.0 bind) mutant except those above.

### Info

| # | Where | Finding | Source | Confidence |
|---|---|---|---|---|
| 8 | tasks.json | ❓ Working tree shows `tasks.json` modified: `last_verify` timestamps rewritten by re-verifying all tasks. Expected side effect, not a defect. | critic | — |

## Auto-fix (`--fix`)

Not run (no `--fix`). Nothing was changed by this check except plan state written by `tasks.sh verify`.

## Ratchet

`docs/sweeps/ratchet.json` still does not exist and its detectors are TypeScript-oriented, so none was created. Python equivalents, informational:

| Metric | Pre-plan main | Current | Δ |
|---|---|---|---|
| type_errors (pyright, venv visible) | 54 | 54 | 0 |
| test_count (pytest collect) | 120 | 525 | +405 |
| TODO/FIXME in source | 0 | 0 | 0 |
| mocks in non-test source | 0 | 0 | 0 |

The skill's absolute type-error floor of 0 makes the 54 pre-existing errors a mechanical gate failure on their own; they are unrelated to this plan.

## Critic

`--mode reject` verdict: LGTM (as returned) · mode: in-Claude (opus, fresh context) · findings: 3 (confirms finding 1; confirms finding 2 unchanged; the dirty `tasks.json`) · held-out: 21 ok, 1 not ok (see Held-out checks for how that interacts with the skill's rule).

## Security posture

registry-validate: ok · startup-validate --strict: non-zero, no injection (one SCHEMA finding on hook-owned `.cc-sessions/typecheck-baseline.json`: `schema_version: 2` vs the fields the validator expects, a plugin-side mismatch) · pre-trust execution: none in project content

## Automation coverage

Deterministic gates: tests pass; typecheck fails on pre-existing debt only (0 introduced); lint and build have no python lane; ratchet not applicable; registry rows clean; critic LGTM with one failing extra held-out check · e2e: skipped_unavailable
Not auto-verified (human owns): behavior against real Gmail, the Anthropic structured-output round trip on real data, triage quality, architectural fit, business intent, and the not-yet-built render phase.
**Recommendation:** needs-human-review

## Before merge

The branch is already merged (PR #1), so this is the list before relying on it against a real mailbox.

1. **Fix finding 1 (P1).** New task: (a) in `_decode_part`, accept only real text codecs (reject `unicode_escape`, `raw_unicode_escape`, `utf-7` and other non-charset codecs, falling back to utf-8/replace) and scrub any surrogate that survives; (b) in `triage_items`, catch every failure from a call (not only `APIError`/`ValidationError`), stub the batch, and record `<ExceptionClass>`; (c) guard the `triage_cli` print paths. Tests: each of the three charsets end to end through `triage_items` with the real SDK client.
2. **Fix finding 2.** Add `handle_startendtag` so a self-closing non-void hidden tag opens a hidden region (with tests for `div`, `hidden`, `aria-hidden`, `script`); pin `head` and `title` separately (finding 7).
3. **Fix finding 4,** and add the three missing assertions (findings 3, 5, 6): hostile From name through `stub_record`, `--dry` writes nothing, `log_message` never prints the code.
4. Run the live dry run: `python -m pipeline.collect gmail_<label> --raw --hours 2 --limit 3`; then the day-8 token check.
5. Decide on the 54 pre-existing pyright errors (SQLModel `col()` wrappers in `db.py`, Optional handling in `runner.py`, narrowing asserts in older tests): they alone keep this gate red.

## Later

1. `strip_quoted_reply` drops everything after the attribution line, so bottom-posted replies lose all text.
2. A model `one_line` that sanitizes to empty is kept as a valid record (blank digest line).
3. `sanitize_text` is quadratic on huge subjects and `stub_record` feeds it uncapped input; `build_request` sends title and participants uncapped, so one hostile email can make a 20-item batch oversized and get it stubbed (conf 0.6); bidi and zero-width characters survive; entity-encoded schemes (`java&#115;cript:`), 2-level markdown parens, fullwidth `ｈｔｔｐ://` and unterminated tags/comments survive (matters only if the renderer decodes entities or markdown).
4. `_call` sleeps ~63 s per retry-exhausted id and `requests.RequestException` is not retried.
5. The port-release test passes only via refcount GC; retain a server reference to make it a real close assertion.
6. README says every bootstrap run mints a token, but a healthy configured token short-circuits; `triage_cli` is not in the README; `to_epoch_seconds` mis-reads an aware datetime.
7. Recorded and pinned by a test: white-on-white / font-size:0 text still reaches the model.
8. Re-record the hook's typecheck baseline and fix its schema mismatch with `startup-validate`; add a python test lane to the toolchain.
