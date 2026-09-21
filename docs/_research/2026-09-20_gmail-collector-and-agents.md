# Gmail collector, and where the synthesis "agents" start

Date: 2026-09-20. Google and Anthropic claims below were fetched from primary docs on this date; anything not confirmed verbatim in a fetched page is marked `UNVERIFIED`. Design proposals (mine, not sourced) are marked `PROPOSAL`.

```yaml
scope:
  topic: gmail-collector-and-agents
  sources: [gmail]
  target_account: consumer @gmail.com (primary); Google Workspace (secondary)
  depends_on:
    - docs/_research/2026-09-13_phase2-collectors.md
    - docs/_research/2026-09-13_auth-approach.md
  status: final
  capabilities:
    - 1 new collector (pipeline/collectors/gmail.py)
    - 1 new auth bootstrap (auth/gmail_bootstrap.py)
    - 1 new health check (check_gmail)
    - Phase 3 architecture decision (agents = plain Messages API workflow, no agent loop)
```

## Summary

Gmail fits the existing collector seam with no new machinery: `messages.list` with an epoch-second `q=after:… before:…`, then sequential `messages.get`, into `Item("mail", …)`. The hard part is **auth, not collection**. A consumer Gmail refresh token dies in 7 days if the OAuth consent screen is left in "Testing", dies on any Google password change, and Gmail-scoped tokens have no service-account escape hatch for `@gmail.com`. The plan is a per-user Desktop OAuth client, consent screen published to "In production", `gmail.readonly` only, bootstrapped once on a machine with a browser, refresh token then held server-side.

For "agents": Phase 3 should be a **plain Messages API workflow** (per-source map calls, deterministic Python merge, one reduce call) using structured outputs. It is not an agent loop, not the Agent SDK, not Managed Agents. Email is untrusted input, and the safest summarizer is one with no tools. Estimated cost is on the order of $12/month (assumption-based, arithmetic in Q7).

Two things to do before writing code: (1) publish the consent screen and verify a token survives past day 8; (2) run the collector's dry run with `--raw` to settle the boundary/ordering questions the docs leave open.

## Research questions

**Q1. Which OAuth flow works for a headless server and a consumer Gmail account?** Installed-app (Desktop client) authorization-code + PKCE with a `127.0.0.1` loopback redirect, run once on any machine with a browser. Device flow cannot carry Gmail scopes, out-of-band copy/paste is dead, service accounts need a Workspace admin.

**Q2. What is the refresh-token lifetime risk?** The dominant operational risk. "Testing" = 7 days. Production-unverified is documented as fine for one user. Password change, revocation, and the 100-token cap also kill tokens.

**Q3. Which scope?** `gmail.readonly` only. `gmail.metadata` is also Restricted and cannot use `q` or bodies, so it saves nothing.

**Q4. How to list and fetch?** One `messages.list` over an epoch-second window, drain all pages, sequential `messages.get`. No `history.list`, no `watch`, no batch.

**Q5. What are the quota and error-handling rules?** ~300 messages/day is ~6,000 units, exactly the per-user per-minute budget, so pace calls. Retry on 403 rate reasons, 429, 5xx.

**Q6. How does it wire into this codebase?** Six places hard-code the source list. See Findings §7.

**Q7. How should the Phase 3 agents be built, and what must collectors emit for them?** Map→reduce over the Messages API; a normalized item schema is a Phase 3 adapter, not a collector concern (with one Gmail-specific exception, §5).

## Findings

### 1. Auth: what works on a headless box

- Device flow excludes Gmail. https://developers.google.com/identity/protocols/oauth2/limited-input-device (updated 2026-09-14): "The OAuth 2.0 flow for devices is supported only for the following scopes:", and the list has no Gmail scope.
- Loopback + PKCE is the supported desktop path. https://developers.google.com/identity/protocols/oauth2/native-app: "http://127.0.0.1:port or http://[::1]:port"; and the loopback option is deprecated only for mobile: "The loopback IP address redirect option is DEPRECATED for Android, Chrome app and iOS OAuth client types."
- Copy/paste is gone: "The manual copy/paste option, also referred to as an out of band (OOB) redirect method, is no longer supported."
- Installed apps always get a refresh token: "Note that refresh tokens are always returned for installed applications."
- Service accounts: https://developers.google.com/identity/protocols/oauth2/service-account describes them for "when a Google Workspace administrator grants domain-wide authority to access user data on behalf of users". No page states "unsupported for @gmail.com"; that conclusion is inference (`UNVERIFIED` as verbatim). Consumer accounts have no admin to grant delegation.
- App password + IMAP is a non-OAuth fallback: https://support.google.com/accounts/answer/185833: "App passwords aren't recommended and are unnecessary in most cases."; "App passwords can only be used with accounts that have 2-Step Verification turned on."; and Google revokes them on password change. https://support.google.com/mail/answer/7126229: "Starting January 2025, the option to choose "Enable IMAP" or "Disable IMAP" won't be available. IMAP access is always turned on in Gmail".

Headless bootstrap (SSH-forwarding the loopback port, or authorizing on a workstation and copying the token) is an implementation note, not a documented recipe (`UNVERIFIED` as "documented"). It mirrors what `auth/m365_bootstrap.py` already does for Microsoft.

### 2. The refresh-token lifetime gotcha

Convergent across ≥3 domains (developers.google.com, support.google.com, n8n community reports):

- Testing = 7 days. https://support.google.com/cloud/answer/15549945: "Authorizations by a test user will expire seven days from the time of consent. If your OAuth client requests an offline access type and receives a refresh token, that token will also expire." Same rule at https://developers.google.com/identity/protocols/oauth2 (updated 2026-05-26), where the only exception is when scopes are a subset of name, email, and profile. Gmail scopes are not in it.
- Publishing removes it. 15549945: "A project's publishing status is considered In production after selecting the Publish app button."
- Personal use is exempt from verification. https://support.google.com/cloud/answer/13464323: "If the app is for your personal use (fewer than 100 users), you and your limited number of users can continue using the app without going through verification(users will be allowed to click through "unverified app" warning screens during sign-in)." Restricted-scope page (updated 2026-08-19): "One use case is if you are the only user of your app or if your app is used by only a few users, all of whom are known personally to you."
- The cap is lifetime, not per-token: 15549945: "The user cap applies over the entire lifetime of the project, and it cannot be reset or changed." One user uses one slot.
- CASA: "Apps accessing restricted data from or through a third-party server must undergo an annual security assessment by a Google-approved third party." A one-user personal app is exempt from verification, so CASA is not triggered. **That is inference**: no page says "CASA not needed for personal use" in those words.

Other kill switches, from https://developers.google.com/identity/protocols/oauth2 (updated 2026-05-26), each quoted:

- "The user has revoked your app's access."
- "The refresh token has not been used for six months." (irrelevant for a daily job unless it stalls)
- "The user changed passwords and the refresh token contains Gmail scopes." **A password change on the Google account kills the unattended token.**
- "There is currently a limit of 100 refresh tokens per Google Account per OAuth 2.0 client ID. If the limit is reached, creating a new refresh token automatically invalidates the oldest refresh token without warning." Re-running the bootstrap repeatedly during development can silently kill a token already copied to the server.
- "You must write your code to anticipate the possibility that a granted refresh token might no longer work."

Error surface: the native-app page names `invalid_grant` ("the token may have expired or has been invalidated"). The exact HTTP status and JSON body of the refresh failure is not quoted on the page (`UNVERIFIED`).

No change to any of these rules was found for 2025–2026. The pages are current to 2026-05…09, and Gmail's release notes list no auth changes in 2026. The only 2026 change is quota (§5).

Caveat with teeth: an anecdotal 2024 Google Ads API forum thread reports a production-status, unverified, personal-use app whose "refresh token keeps expiring" (https://groups.google.com/g/adwords-api/c/WDgwEZT6Cd0). Google's reply did not resolve it. The docs say only Testing expires. **Verify empirically at day 8+.**

Workspace (secondary): an "internal" consent screen is exempt from the unverified screen and the 100-user cap: "Your app will not be subject to the unverified app screen or the 100-user cap if it's designated as internal-only." (13464323; project must be owned by the org). Admins can still block via `admin_policy_enforced`, and app passwords are unavailable for org accounts (185833). Domain-wide delegation exists for Workspace but needs a super-admin and can impersonate any user, so it is too much blast radius for a digest reader.

### 3. Scope

https://developers.google.com/workspace/gmail/api/auth/scopes (updated 2026-09-10):

| Scope | Class | Reads message bodies? | `q` search? |
|---|---|---|---|
| `gmail.readonly` | Restricted | yes | yes |
| `gmail.metadata` | **Restricted** | no | no |
| `gmail.modify` | Restricted | yes | yes (and writes; don't) |
| `gmail.labels` | Non-sensitive | no | n/a |

`gmail.metadata`: `users.messages.list` marks `q` "Parameter cannot be used when accessing the api using the gmail.metadata scope." and the discovery doc says the same for `format=FULL`/`RAW`. Both from the API reference (updated 2026-04-15) and the v1 discovery document (rev 20260917). No non-restricted scope reads message content. **Request `gmail.readonly` and nothing else.** Assert it appears in the token response's `scope` field at bootstrap.

### 4. Listing and fetching

**Listing** (https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list):

- `maxResults`: "This field defaults to 100. The maximum allowed value for this field is 500." Use 500.
- Ids only: "Note that each message resource contains only an id and a threadId." One `get` per message.
- Time zones: https://developers.google.com/workspace/gmail/api/guides/filtering: "All dates used in the search query are interpreted as midnight on that date in the PST timezone. To specify accurate dates for other timezones pass the value in seconds instead". **Use epoch seconds, never date strings.**
- Not documented (`UNVERIFIED`): inclusive/exclusive boundary of `after:`/`before:` epochs, list ordering (newest-first), and whether `newer_than:` accepts hours (the search-operator page lists only d/m/y). Overlap the window, window on `internalDate` client-side, dedupe by message id.
- `resultSizeEstimate` is "Estimated total number of results." Don't rely on it.
- Practitioner reports (search-snippet only, not page-verified): the list index is eventually consistent, and list ordering is not guaranteed newest-first (an open issue on the Node client, googleapis/google-api-nodejs-client#3508). Both support the same two rules: drain every page, overlap the window.

**`history.list` and `watch` are not worth it here.** https://developers.google.com/workspace/gmail/api/guides/sync: "History records are typically available for at least one week and often longer. However, the time period for which records are available might be significantly shorter" and, past that, "your client must perform a full sync." That is a persisted cursor plus a full-sync fallback, the exact machinery `phase2-collectors.md` rejected for Graph. `watch` (https://developers.google.com/workspace/gmail/api/guides/push) requires "You must call the watch method at least once every 7 days", a Cloud Pub/Sub topic with an IAM grant, and still says "fall back to periodically calling the history.list method". A timestamp read self-heals; these don't.

**Fetching** (`users.messages.get`, discovery rev 20260917): `format=full` (default) returns "body content parsed in the `payload` field"; `metadata` returns "only email message ID, labels, and email headers" and honors repeated `metadataHeaders`; `minimal` has no headers or body. Bodies are `payload.parts[].body.data`, "a base64url encoded string". Whether `metadata` returns `snippet` is `UNVERIFIED`. Attachments have `attachmentId` and `filename`; do not fetch them (`attachments.get` is 20 units), record name/size only. Prefer `messages.get` over `threads.get` (20 vs 40 units).

`internalDate`: "For normal SMTP-received email, this represents the time the message was originally accepted by Google, which is more reliable than the Date header. However, for API-migrated mail, it can be configured by client to be based on the Date header." **Window on `internalDate`, not `Date`.** It is a millisecond-epoch JSON string.

Labels (https://developers.google.com/workspace/gmail/api/guides/labels): `CATEGORY_PERSONAL/SOCIAL/PROMOTIONS/UPDATES/FORUMS` plus `INBOX`, `SPAM`, `TRASH`, `UNREAD`, `STARRED`, `IMPORTANT`, `SENT`, `DRAFT`. "The preceding list isn't exhaustive and other reserved label names exist." Not documented: MIME `multipart/alternative` selection (client logic) and charset handling (`UNVERIFIED`).

### 5. Quota, errors, and why not batch

https://developers.google.com/workspace/gmail/api/reference/quota (updated 2026-09-10):

- **New in 2026:** "As of May 1, 2026, the usage limits for this API were updated. Google Cloud projects that made any use of this API between November 2025 and April 2026 will continue with their previously set usage quotas. Cloud projects created on or after May 1, 2026 are subject to the new API quotas." The release notes (May 01 2026): "Quota units for drafts.get, messages.attachments.get, messages.get, messages.trash, threads.get, and threads.trash have changed." A GCP project created for this app is on the new quotas.
- Units: `messages.list` 5, `messages.get` 20, `threads.get` 40, `getProfile` 1, `history.list` 2, `watch` 100.
- Limits: "Per minute per user per project 6,000 quota units"; "Per minute per project 1,200,000 quota units"; "Per day per project 80,000,000 quota units". Also: "Exceeding the quota request limits is planned to incur charges to your Google Cloud billing account later in 2026."
- Arithmetic (mine): 300 `get` × 20 = 6,000 units, the entire per-user per-minute budget. Pace at ≤~200 gets/minute or lean on backoff; do not burst.
- Retry: https://developers.google.com/workspace/gmail/api/guides/handle-errors (updated 2026-09-15): `rateLimitExceeded` / `userRateLimitExceeded` are HTTP **403**, not just 429; "A 429 "Too many requests" error can occur due to daily per-user limits… bandwidth limits, or a per-user concurrent request limit." and "Making many parallel requests for a single user or sending batches with a large number of requests can trigger this error." **Do not parallelize.** Truncated exponential backoff (formula on the quota page above, not handle-errors): "min(((2^n)+random_number_milliseconds), maximum_backoff)", maximum typically 32 or 64 seconds. Retry on 403 with a rate reason, 429, and 500/502/503/504.
- **Batch has the same envelope failure mode as Graph.** The docs' batch page says each part "contains a complete HTTP response, including a status code", and the agent observed an unauthenticated POST to `https://gmail.googleapis.com/batch` return outer HTTP 200 with an inner "HTTP/1.1 401 Unauthorized" (OBSERVED, not a doc claim). Quota cost is identical: "A set of n requests batched together counts toward your usage limit as n requests, not as one request." Google recommends "batches of no more than 50 requests". Batching saves seconds and adds multipart parsing plus per-part retry. Skip it, for the same reason `phase2-collectors.md` §2 skipped `$batch`. The global `https://www.googleapis.com/batch` endpoint is not a working path: one probe returned 404, a later one 400 (both OBSERVED, not doc claims); the per-API path responds.

### 6. Health check

- Refresh with plain `requests`, no client library needed: POST `https://oauth2.googleapis.com/token`, form-encoded `client_id`, `client_secret`, `grant_type=refresh_token`, `refresh_token`. "A successful exchange is indicated by a 200 OK response containing a new access token." **No new refresh token is returned**, so unlike MSAL there is no cache to write back. Whether a Desktop client can omit `client_secret` is `UNVERIFIED`; send it.
- The response `scope` field gives granted scopes ("expressed as a list of space-delimited, case-sensitive strings"), and the page says "Your app must verify which scopes were actually granted". That is the analogue of Slack's `x-oauth-scopes` and Zoom's `scope` checks in `pipeline/health.py:27-41`.
- Cheapest live probe: `GET https://gmail.googleapis.com/gmail/v1/users/me/profile` (1 unit, accepts `gmail.readonly`).
- Semantics (PROPOSAL): refresh 200 + profile 200 = ok; `invalid_grant` = hard error, no retry, message names the likely causes and `auth/gmail_bootstrap.py <label>`; 403-rate/429/5xx = transient. `tokeninfo` is documented only for `id_token`; don't use it.

### 7. The seam in this codebase (verified by reading the code this session)

The source list is hard-coded in **six** places, not one. Adding Gmail means touching all of them or it silently half-works:

| Where | What | Change |
|---|---|---|
| `pipeline/health.py:66` | `_env()` key-prefix filter | add `"GMAIL_"` |
| `pipeline/health.py:252-254` | `known_sources()` | append `gmail_<label>` |
| `pipeline/health.py:257-272` | `check_all_configured()` | branch calling `check_gmail` |
| `pipeline/runner.py:134-137` | `_active()` | **duplicates** `known_sources()` instead of calling it; use `known_sources()` and drop the duplicate |
| `pipeline/collectors/__init__.py:79-98` | `dispatch()` | `gmail_` branch, local import like the others |
| `tests/conftest.py:17` | `CONFIG_PREFIXES` | add `"GMAIL_"`, or a developer's real env leaks into tests (the failure that fixture was written for) |

Also `pipeline/db.py:47,61` and `CollectedItem` comments list source-id shapes; update the comments. `pipeline/config_store.py:25-27` and `pipeline/collect.py:92` already go through `known_sources()`, so they pick Gmail up for free, and the web UI needs no change.

Reusable as-is: `Item`/`CollectionResult` (`collectors/__init__.py:27-45`), `parse_iso`, `OVERLAP`, `MAX_BACKFILL`, the runner's cursor + backfill + `MAX_STUCK_RUNS` machinery (`runner.py:45-64`, `:38`), and `pipeline/collect.py`'s dry run. `PREVIEW_FIELDS` (`collect.py:30`) already includes `subject`, so a Gmail payload with a top-level `subject` previews correctly.

**Payload shape** (PROPOSAL, a deliberate small deviation). `db.py:52-55` says items are stored raw and normalized in Phase 3, and Slack/Graph pass fields through. Gmail's raw form is a nested base64url MIME tree with both `text/plain` and `text/html` copies of the body. Decoding is source-specific parsing, so it belongs in the collector. Store a flattened payload: `subject`, `from`, `to`, `cc`, `date`, `messageId`, `inReplyTo`, `listUnsubscribe`, `threadId`, `labelIds`, `snippet`, `internalDate`, `bodyText` (decoded, `text/plain` preferred, HTML stripped otherwise, capped, with a `bodyTruncated` flag), `attachments[{filename,mimeType,size}]`. Sizes are not measured; read them from the dry run before picking the cap.

### 8. Phase 3 "agents" (from `agents-analyst`)

**Architecture: plain Messages API workflow, not an agent loop.**

- Anthropic's own guidance, https://www.anthropic.com/engineering/building-effective-agents: "you should consider adding complexity _only_ when it demonstrably improves outcomes". Map→reduce is their "sectioning" parallelization. Source: a summarizer relay, so quote is as returned by the fetch tool, not raw page text.
- Native structured outputs exist and are GA: https://platform.claude.com/docs/en/build-with-claude/structured-outputs: "The `output_format` parameter has moved to `output_config.format`, and beta headers are no longer required." The Python `parse()` helper "automatically transforms your Pydantic model, validates the response, and returns a `parsed_output` attribute." Not supported in schemas: numeric constraints (`minimum`/`maximum`), string length constraints, recursive schemas. Validate ranges in Pydantic after parsing.
- Agent SDK is a separate package (`claude-agent-sdk`) and "gives you the same tools, agent loop, and context management that power Claude Code" (https://code.claude.com/docs/en/agent-sdk/overview). Nothing here needs a tool loop.
- Managed Agents (https://platform.claude.com/docs/en/managed-agents/overview) is beta, needs the `managed-agents-2026-04-01` header, and "is not currently eligible for Zero Data Retention or HIPAA Business Associate Agreement (BAA) coverage." Per the pricing page (https://platform.claude.com/docs/en/about-claude/pricing) it bills "$0.08 per session-hour" and has no batch mode. Email content held server-side is a poor fit for a privacy-minded self-hosted tool.
- Citations feature can't provide provenance here: "Citations cannot be used together with structured outputs… the API returns a 400 error." Provenance must be self-managed: give the model short per-run aliases (`m001`), map back to real ids in Python, render permalinks from our own id→URL map. The model never emits a URL.

**Models and cost** (https://platform.claude.com/docs/en/models/overview and the pricing page, fetched raw): `claude-sonnet-5` $2 in / $10 out per MTok; `claude-haiku-4-5-20251001` $1 / $5. The pricing page says the scheduled Sonnet 5 increase to $3/$15 "will not occur." Haiku 4.5's deprecations row reads "Active … Not sooner than October 15, 2026", with "at least 60 days' notice before model retirement". **Make model ids config, and re-check the deprecations page before building.**

Estimate (assumptions are mine, not sourced): ~200 emails × ~660 tokens + ~100 chat/calendar items × ~150 → ~170k map input tokens/day; ~27k map output; reduce ~33k in / ~4k out.

| Plan | Per day | Per month (30d) |
|---|---|---|
| Haiku 4.5 map + Sonnet 5 reduce, sync | ~$0.41 | ~$12 |
| Same, Message Batches (50% off) | | ~$6 |
| Sonnet 5 for both (+30% tokenizer uplift on map) | ~$0.90 | ~$27 |

Plausible worst case (double volume, retries, thinking overhead) is ~$32–70/month. Prompt caching is ~$1/month and does nothing on Haiku (min cacheable 4,096 tokens vs a ~1.5k prompt). Batch saves ~$6–13/month but adds a 24h expiry risk that conflicts with "degrade gracefully". **Go sync.**

**Prompt-injection posture.** Anthropic's guardrails page names the threat: "Indirect prompt injection, where the user is trusted but Claude processes *third-party content* (web pages, emails, documents, tool results) that contains adversarial instructions." (https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks). Documented mitigations that apply: JSON-encode every third-party string rather than concatenating into tags (`json.dumps` each item; an email body containing `</document>` breaks tag delimiting); label the content as an inbound email from an unknown sender; a policy paragraph saying it is data; no tools, network, or secrets in the summarizer; optionally a cheap Haiku pre-screen. Simon Willison's "lethal trifecta" (https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/, 2025-06-16) frames why: private data + untrusted content + an outbound channel. Removing the third leg is what contains this design. OWASP LLM01 concedes "it is unclear if there are fool-proof methods of prevention for prompt injection." Residual risk in a no-tool digest is a misleading digest or fake todo, so treat all model output as untrusted at render: no HTML passthrough into the PDF, no URLs outside our map, no auto-fetched images. Never let a digest item trigger an action.

Tension worth prototyping, not resolving on paper: Anthropic's page recommends delivering untrusted content in `tool_result` blocks, which needs a fake forced tool and two turns. Whether that composes with `output_config.format` in one flow is `UNVERIFIED`.

**Normalized item** (PROPOSAL, no published digest schema found, `UNVERIFIED` as sourced): a Phase 3 adapter (payload JSON → `NormalizedItem`) with `id`, `source`, `kind`, `occurred_at`, `title`, `body_text`, `participants`, `thread_key`, `labels`, `direction`, `permalink`, `attachments`, `raw_ref`. The LLM sees only `{alias, kind, occurred_at, title, participant names, truncated body, labels}`. Adapters are pure functions with fixture tests. For Gmail: `thread_key` = `threadId`; `permalink` is derivable from the message id (format not sourced; verify).

**Testing.** Anthropic's eval guidance: "Design evals that mirror your real-world task distribution." (https://platform.claude.com/docs/en/test-and-evaluate/develop-tests) and use a different model as judge. Constraint that shapes CI: `temperature`/`top_p`/`top_k` return a 400 on Claude 4.7-and-later models (deprecations page, raw), so Sonnet 5 output is not reproducible; assert structure and properties (schema parses, every returned alias is in the input set, every action item has `source_ids`, an injection canary never appears in output), never exact text. Layer: pure-Python tests → recorded-response fixtures → a small live eval set (20–50 cases, incl. injection samples), run manually or nightly, not per-PR.

## Dissent / Contradictory Evidence

1. **"Production-unverified tokens last" is documented but not proven.** Google's docs say only Testing expires; one forum anecdote (§2) says otherwise. The plan has no fallback if it's true. Mitigation is step 0 below; the fallback is IMAP + app password.
2. **IMAP + app password may be more reliable than OAuth for one user.** No expiry clock, and Google's transition page (https://knowledge.workspace.google.com/admin/sync/transition-from-less-secure-apps-to-oauth) says of the March 2025 cutoff: "You will no longer use a password for access (with the exception of app passwords)." Against that: Google's own support page calls app passwords "not recommended", they need 2-Step Verification, they die on password change too, and they grant the whole mailbox, a broader grant than `gmail.readonly`. Community reports of app-password IMAP failing for some accounts (search snippets: paperless-ngx discussion #4557, Huginn #3488, Home Assistant core #93413) are `UNVERIFIED`. Not built; keep as the documented fallback.
3. **Fixed-window fragility (web-researcher) is already handled.** It argued for a persisted watermark plus overlap. `SourceCursor`, `OVERLAP`, and `MAX_BACKFILL` already do exactly that (`runner.py:45-64`). No action.
4. **Agent disagreement on device flow:** `web-researcher` had it as "[UNVERIFIED, memory]". `library-docs` confirmed from the page. Resolved, settled by the primary source.
5. **A shared client id in the public repo is a trap.** The restricted-scope page says an app "accessing restricted data from or through a third-party server must undergo an annual security assessment". Personal-use exemption applies to a user's own project. The README must tell each user to create their own GCP project and OAuth client, and no client id may ship in the repo.
6. **Every digest feature that acts breaks the injection story.** "Email me the digest" or "create calendar events from todos" reinstates the lethal trifecta.

## Risks

- **Silent daily failure of auth.** A dead token is discovered the next morning, when the digest is missing Gmail. Mitigate: health check runs before collection (already the flow at `runner.py:221-227`), `invalid_grant` gets its own loud message, and the dashboard already surfaces per-source errors.
- **Google password change** invalidates the token with no warning. Document it in the README next to the M365 revocation list.
- **Re-bootstrap eviction:** repeated auth during development can invalidate a token already deployed (100-token cap).
- **Quota tightness:** 300 gets = the full per-minute budget; a burst hits 403/429. Pace, back off, don't parallelize.
- **Late/backdated mail** (imports, `internalDate` set by API-migrated mail) can fall outside an `after:` window. Overlap plus dedupe covers normal cases; not fully closeable (`UNVERIFIED`, no report found).
- **Haiku 4.5 retirement** (not sooner than 2026-10-15) lands during Phase 3 development. Config-driven model ids and a golden set are the migration test.
- **Third-party email leaves the LAN** to the LLM vendor. Retention terms for the chosen API tier were not checked (`UNVERIFIED`).

## Open questions

- Does a production-unverified consumer token actually survive past day 8? (empirical)
- Boundary semantics of `after:`/`before:` epoch seconds; list ordering. (dry run)
- Does `format=metadata` return `snippet`? Does `q=-in:draft` exclude drafts? (dry run)
- Include SENT mail, or inbound only? Default `q` includes both; `labelIds` in the payload lets Phase 3 decide. Leaning include and tag.
- Category noise: collect Promotions/Social too, or filter with `-category:promotions` at query time? Filtering saves 20 quota units per dropped message; not filtering loses nothing recoverable. Decide after seeing real volume.
- Does the `tool_result` delivery pattern compose with structured outputs?
- Should per-source settings (lookback, label filter) move into `config.json`? Same open question as `phase2-collectors.md`.

## Recommendation

### Decision

Build the Gmail collector as `gmail_<label>`, one Google account per label, using a per-user Desktop OAuth client, `gmail.readonly` only, plain `messages.list` (epoch seconds) + sequential `messages.get`, no batch, no `history.list`, no `watch`. Before writing any collector code, publish the consent screen to "In production" and start the day-8 token-lifetime clock. For Phase 3, build a plain Messages API map→reduce workflow with structured outputs and no tools; start by defining the `NormalizedItem` adapter using Gmail as the first source.

### Rationale

- The refresh-token rules are documented and stable across 2026 pages, and they, not the API, are where a personal Gmail automation actually breaks. Starting the lifetime test first costs nothing and de-risks everything after it.
- `gmail.readonly` is the smallest scope that supports both `q` and bodies. `gmail.metadata` is also Restricted, so it buys no verification relief.
- The batch, history, and watch machinery all add a failure path (envelope-hidden 429s, 404 full-sync, Pub/Sub + IAM + daily renewal) for a once-daily, ~300-message pull where a timestamp read already self-heals through the existing cursor.
- A workflow beats an agent loop for a batch summarizer: no tool surface removes the third leg of the lethal trifecta, and Anthropic's own guidance is to add complexity only when it demonstrably improves outcomes.
- Doing the Gmail adapter first grounds the `NormalizedItem` schema in a real, messy source (MIME, threads, labels) before Graph/Slack/Zoom adapters.

### Config shape (PROPOSAL, mirrors Slack)

```
GMAIL_CLIENT_ID=            # Desktop OAuth client, your own GCP project
GMAIL_CLIENT_SECRET=
GMAIL_PERSONAL_REFRESH_TOKEN=   # label -> source id gmail_personal; any number of labels
```

Refresh tokens don't rotate (the refresh response returns no new one), so a Slack-style `.env` secret needs no write-back and no cache file. `auth/gmail_bootstrap.py <label>` runs loopback + PKCE on a machine with a browser, asserts `gmail.readonly` is in the granted scopes, and prints the token to paste into `.env`. Pattern: `^GMAIL_([A-Z0-9]+)_REFRESH_TOKEN$` (does not collide with `GMAIL_CLIENT_ID`).

## Implementation sketch

Step 0 (before any code): create a GCP project, enable the Gmail API, configure the consent screen as External, add `gmail.readonly`, **Publish app** to In production, create a Desktop client. Authorize once; record the date. Re-check the token on day 8+.

1. `auth/gmail_bootstrap.py` — loopback + PKCE flow, granted-scope assertion, print token. Mirror `auth/m365_bootstrap.py`'s shape (it short-circuits to the health check when already good).
2. `pipeline/health.py` — `GMAIL_` in `_env()` prefix filter (`:66`); `_GMAIL_TOKEN_KEY` regex and `gmail_accounts()` alongside `slack_workspaces()` (`:88`, `:102`); `gmail_access_token()` (plain POST, returns token + granted scopes); `check_gmail()` (refresh → scope assert → `/profile`); wire into `known_sources()` (`:252`) and `check_all_configured()` (`:257`).
3. `pipeline/runner.py:134-137` — replace the duplicated list in `_active()` with `known_sources()`.
4. `pipeline/collectors/gmail.py` — `_call()` with the Slack collector's error-to-exception shape and backoff on 403-rate/429/5xx; `_list_ids()` (epoch `q`, `maxResults=500`, drain pages, raise at `MAX_PAGES` like `graph.py:109-112` rather than truncate); `_fetch()` (sequential `get` on one `requests.Session`, paced); `_flatten()` (headers, base64url decode with padding, `text/plain` preferred, stdlib-`html.parser` fallback, cap + `bodyTruncated`); client-side `internalDate` window filter; `collect_gmail(label, refresh_token, since, until) -> CollectionResult` with `partial` when some `get`s fail and `error` on `invalid_grant`.
5. `pipeline/collectors/__init__.py:79-98` — `gmail_` branch in `dispatch()`.
6. `tests/conftest.py:17` — add `"GMAIL_"`. `tests/test_health.py` — token-key regex, `known_sources` ordering, scope-missing and `invalid_grant` cases. `tests/test_collectors.py` — follow the existing `FakeResponse`/`route` helpers (`:25`, `:38`): pin the `q` string (epoch seconds, never dates), pagination and `MAX_PAGES` raise, `internalDate` window rejection, base64url padding, `multipart/alternative` selection and HTML fallback, 403-rate then success, `invalid_grant` → error, same-id dedupe.
7. README — Gmail setup section (GCP project, In production, unverified click-through, password-change warning, "create your own client; never commit one"), `.env.example` entries, Phases table.
8. Dry run: `python -m pipeline.collect gmail_<label> --raw --hours 2 --limit 3`. Use it to settle boundary, ordering, snippet, draft, and payload-size questions before the first scheduled run.
9. Phase 3, first slice only: `NormalizedItem` + a Gmail adapter (pure function, fixture tests), then a single map call over Gmail items with `messages.parse()`. Stop there before designing the full reduce.

## References

- https://developers.google.com/identity/protocols/oauth2
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/identity/protocols/oauth2/limited-input-device
- https://developers.google.com/identity/protocols/oauth2/service-account
- https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- https://support.google.com/cloud/answer/15549945
- https://support.google.com/cloud/answer/13464323
- https://support.google.com/accounts/answer/185833
- https://support.google.com/mail/answer/7126229
- https://knowledge.workspace.google.com/admin/sync/transition-from-less-secure-apps-to-oauth
- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
- https://developers.google.com/workspace/gmail/api/guides/filtering
- https://developers.google.com/workspace/gmail/api/guides/sync
- https://developers.google.com/workspace/gmail/api/guides/push
- https://developers.google.com/workspace/gmail/api/reference/quota
- https://developers.google.com/workspace/gmail/api/guides/handle-errors
- https://developers.google.com/workspace/gmail/api/guides/batch
- https://developers.google.com/workspace/gmail/api/guides/labels
- https://developers.google.com/workspace/gmail/release-notes
- https://gmail.googleapis.com/$discovery/rest?version=v1
- https://groups.google.com/g/adwords-api/c/WDgwEZT6Cd0
- https://github.com/googleapis/google-api-nodejs-client/issues/3508
- https://www.anthropic.com/engineering/building-effective-agents
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- https://platform.claude.com/docs/en/build-with-claude/citations
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/models/overview
- https://platform.claude.com/docs/en/about-claude/model-deprecations
- https://platform.claude.com/docs/en/managed-agents/overview
- https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks
- https://platform.claude.com/docs/en/test-and-evaluate/develop-tests
- https://code.claude.com/docs/en/agent-sdk/overview
- https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
- https://genai.owasp.org/llmrisk/llm01-prompt-injection/
