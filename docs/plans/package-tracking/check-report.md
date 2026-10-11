---
result: FAIL
ts: 2026-10-11T00:02:48Z
ref: ec5ab1c50f7308d4e80e7d86f8c6632c9c7781cd
scope: plan
plan: package-tracking
---

# Check: package-tracking

**Verdict: FAIL.** Gates, ratchet and all 23 task verifies are green. The critic REJECTs on one spec violation: packages are polled every 4 hours in practice, not every 2.

## Gates

| Gate | Result |
|---|---|
| pytest (full) | 3450 passed, 3 skipped |
| vitest (full) | 525/525 |
| nuxt typecheck | 0 errors |
| nuxt build | ok |
| ruff F/E9 (new code) | clean |
| mypy (advisory, no repo gate) | SQLModel/narrowing false positives only |
| tasks.sh verify | 23/23 PASS |
| critic held-out | 22/23 ok (T-014 fails) |

## Ratchet

| Metric | Before | After |
|---|---|---|
| test_count (web) | 414 | 438 |
| type_errors | 0 | 0 |
| as_any_count | 0 | 0 |
| mocks_in_src | 0 | 0 |
| todo_count | 0 | 0 |
| stale_worktree_branch_count | 3 | 0 |

## Findings

| Sev | Where | Problem | Fix |
|---|---|---|---|
| P0 (critic) | pipeline/packages/poll.py:83-108 | `next_check_at = now + 2h` with a 2-hourly cron. A run that starts seconds early skips the package, so the effective cadence is 4h (spec Outcome 3). The `poll_interval_hours` and `stop_after_delivered_hours` settings are ignored. | Treat a package as due within a tolerance (for example `next_check_at <= now + 10 min`), and read the settings. |
| P2 | api/routers/packages.py:214 | A date-only ETA stored as midnight UTC is shifted into the local zone, so it lands a day early in US zones. The summary then disagrees with the digest. | Compare `eta.date()` directly for date-only ETAs, or store ETA as a date. |
| P2 | pipeline/packages/carriers/ups.py:66 | UPS activity times are local scan-site times stored as naive UTC. | Use the UPS GMT fields when present; otherwise mark the time approximate. |
| P2 | api/routers/packages.py:286-297 | `add_package` detects duplicates by counting all ids before and after (O(n), racy). An IntegrityError returns 500. | Look up (carrier, number) before inserting; catch IntegrityError and return 409. |
| Major (survey) | web/pages/packages.vue:278 | A rapid filter change can let an older response overwrite newer rows. | Add a request sequence guard. |
| Major (survey) | web/components/PackageDetailDialog.vue:170 | A getPackage response can arrive after packageId changes and show the wrong package. | Check the captured id after await. |
| Minor | carriers/base.py:341 | The rate limiter and OAuth token are rebuilt on every run. | Cache the adapters at module level. |
| Minor | poll.py:111-124 | Generic errors keep polling a failing carrier; rate-limit and auth errors are not recorded on the package. | Stop the carrier after N consecutive errors and set last_error. |
| Minor | inventory.py:279 | mark_stale flip-flops with polling. | Base stale on last_checked_at or carrier activity. |
| Minor | api/routers/packages.py:85 | No max length or charset check on tracking_number or q. | Add a Field(max_length) and a charset check. |
| Minor | tests | The s10 blanket skip; the invalid-number test cannot fail for checksumless formats; the reference and poll fixture keys are not asserted. | Tighten the assertions. |
| Info | det-13 | 2 pytest.skip: the noise case is justified (filtered at inventory.apply_email:204); the s10 rationale is partial. | none |

## Security posture

- Security lens: secrets, XSS, CSRF and email privacy are clean. The UPS/USPS URL path is not URL-encoded, but a full-match format check blocks injection.
- Posture gate: 2 plugin-side hits, neither caused by this plan:
  - critic-external.sh `eval` of constant var names (not project content).
  - .cc-sessions/test-journal.meta.json schema (counters only).

## Not auto-verified

- Architecture fit, UX sense and business intent.
- Real Gmail and carrier API behaviour (stubbed in tests).
- e2e_coverage: skipped (no running app probe).

## Recommendation

needs-human-review. Fix the P0 and the P2s, then re-run check.
