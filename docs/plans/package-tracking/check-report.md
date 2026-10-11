---
result: CONDITIONAL
ts: 2026-10-11T01:48:02Z
ref: dc26e87d2d21f36ff6c0fedf4816fa2c8b2e938e
scope: plan
plan: package-tracking
---

# Check: package-tracking (after --fix)

**Verdict: CONDITIONAL.** The critic returned LGTM and 28/28 held-out checks pass. All gates are green, all 28 tasks verify, and the ratchet has no regression. The verdict is capped by one posture finding in the blitz plugin's own state file, not in project code. Only P3 advisories remain.

## Fix round

The previous run failed on a critic REJECT (P0 poll cadence) plus three P2s and two survey majors. Five tasks from that check fixed them:

| Task | Fix |
|---|---|
| T-024 | Packages count as due 10 minutes early, so a run that starts a few seconds early no longer skips them. The poll interval and stop-after-delivered values come from the settings object. Adapters are cached and rebuilt when credentials change. A carrier stops after 3 consecutive failures, and auth or rate-limit errors are recorded in last_error. |
| T-025 | arriving_today compares eta.date() with the local today. add_package looks for an existing row instead of counting all rows; a duplicate, an archived duplicate or an IntegrityError race returns 409. Inputs have bounds (422). The latest event per package is loaded in one query instead of one per row. |
| T-026 | UPS activity times use the gmt fields or gmtOffset and are stored as UTC. delivered_at matches the activity's local date. |
| T-027 | Sequence guards on the page list load and the detail dialog load. The dialog resets on close. Enter on the track link no longer opens the row. |
| T-028 | S10 country codes are validated against the vendored data. The invalid-number test is stricter. The S10 and noise skips became real assertions. Two strict, commented xfails cover DPD's mod-37/36 checksum, which the matcher does not implement. |

## Gates

| Gate | Result |
|---|---|
| pytest (full) | 3469 passed, 2 xfailed |
| vitest (full) | 528/528 |
| nuxt typecheck | 0 errors |
| nuxt build | ok |
| tasks.sh verify | 28/28 |
| critic held-out | 28/28 ok |

## Ratchet

| Metric | Before | After |
|---|---|---|
| test_count (web) | 438 | 441 |
| type_errors | 0 | 0 |
| as_any / mocks_in_src / todo | 0 | 0 |

## Remaining findings (P3, advisory)

| Where | Problem |
|---|---|
| pipeline/packages/__init__.py:103 | poll_interval_hours and stop_after_delivered_hours have no env key. The spec fixes 2h and 48h, so the defaults are correct. |
| pipeline/packages/poll.py:81 | A scan that runs more than 10 minutes longer than the next one (the first 60-day backfill) can skip one cycle, then recovers. |
| api/routers/packages.py:136 | After a lost insert race, _existing() looks up by number only, which can pick the wrong row for the 409 message. The status code is still correct. |
| web/components/PackageDetailDialog.vue:190 | Closing and reopening on the same id can drop loading early. actionError is not sequence-guarded. |
| pipeline/packages/carriers/base.py:176 | The adapter cache key holds credentials only. |
| pipeline/packages/inventory.py:279 | mark_stale can flip back to in_transit while the carrier reports no new events. |

## Security posture

- Security lens: secrets, XSS, CSRF and email privacy are clean.
- startup-validate exits 2 on a schema check of .cc-sessions/test-journal.meta.json, the plugin's own counters file. No injection content. This caps the verdict at CONDITIONAL.
- The critic-external.sh `eval` takes constant var names only and is plugin-side.

## Not auto-verified

- Architecture fit and UX.
- Live Gmail and carrier API behaviour (stubbed in tests).
- e2e_coverage: skipped (no running app probe).

## Recommendation

needs-human-review. The code is ready to merge. The remaining items are P3 or plugin-side.
