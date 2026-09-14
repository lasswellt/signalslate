# Auth approach across all SignalSlate sources

Date: 2026-09-13. Every claim verified against primary docs on this date; unverifiable items flagged per section.

```yaml
scope:
  topic: auth-approach
  sources: [m365, mstodo, zoom, slack, remarkable]
  supersedes: README "Phase 1 — Auth plumbing" sections 1-5
  status: final
```

## Summary of decisions

| Source | Flow | Bootstrap | Steady state | Expiry / revocation risk |
|---|---|---|---|---|
| M365, N tenants (mail, calendar, To Do, Teams chat) | Delegated, public client, auth-code + PKCE | Once per tenant on a laptop via `acquire_token_interactive`; copy MSAL cache to server | Daily `acquire_token_silent`; RT self-renews (until-revoked, 90d inactivity) | Password change / admin revoke / CA sign-in-frequency / Token Protection |
| Zoom | Server-to-Server OAuth, `account_credentials` grant | Create app as account **owner** | Fetch fresh 1h token every run; no refresh token | Only if app deactivated or scopes removed |
| Slack, N workspaces | User token `xoxp`, non-distributed app per workspace | "Install to workspace" once | Tokens never expire; **do not enable rotation** | Workspace admin revoke / app-approval policy |
| reMarkable | `ddvk/rmapi` device token | Pair once via my.remarkable.com/device/browser/connect (RM2 must already be linked) | User token auto-minted from device token | Cloud API churn (3 breaks in 2025-26) |

Key corrections to current README/code:

1. **Drop device code flow as primary.** Microsoft recommends blocking it; a Microsoft-managed CA policy "Block device code flow" auto-enables ≥30d after landing in report-only. Worse: an RT minted via device code is "protocol tracked" and dies with `AADSTS530036` when the policy later turns on, even though daily refreshes don't use device code. Bootstrap with auth-code+PKCE instead.
2. **Add `Chat.Read`** to `SCOPES` now (Teams 1:1/group chat, no admin consent by default). `ChannelMessage.Read.All` needs admin consent; add only if channel posts are wanted. Adding scopes later forces re-consent in every tenant.
3. **Do not pass `offline_access`** explicitly; MSAL hardcodes it.
4. **Slack: drop all search scopes.** Use `users.conversations` + `conversations.history(oldest=…)` + `conversations.replies`. Add `groups:history`, `channels:read`, `groups:read`, `im:read`, `mpim:read`, `users:read`. Keep each app single-workspace and **never enable distribution** or you fall under the 1 req/min non-Marketplace cap.
5. **Zoom: use `GET /meetings/{uuid}/meeting_summary`** (non-master), parse `summary_content` (Markdown; `summary_overview`/`summary_details`/`next_steps` are deprecated). Enumerate yesterday's meetings via `GET /users/{userId}/meeting_summaries?time_filter_field=summary_created_time` (new Aug 2026) or `GET /report/users/{userId}/meetings?type=past`, not `/users/{id}/meetings`.
6. **reMarkable: target `ddvk/rmapi` v0.0.35+**, not platinummonkey (archived 2025-12). Pairing URL is `/device/browser/connect`, not `/device/desktop`. Python libs (rmapy, rmcl) are dead; shell out to the Go binary.

---

## 1. Microsoft 365 (multiple tenants) + MS To Do

### 1.1 Delegated vs application

**Decision: delegated.** Only model that reads Teams chat without Microsoft protected-API approval and needs no admin consent for the four read scopes in default-consent tenants.

- Graph permission reference (admin consent, delegated / application): Mail.Read No/Yes · Calendars.Read No/Yes · Tasks.Read No/– · Tasks.Read.All Yes/Yes · Chat.Read No/– · Chat.Read.All –/Yes · ChannelMessage.Read.All Yes/Yes.
  https://learn.microsoft.com/en-us/graph/permissions-reference
- Application-permission scoping to one mailbox is via Exchange **RBAC for Applications** (replaces ApplicationAccessPolicy); covers Mail/Calendars/Contacts/MailboxSettings only, nothing for Teams chat or To Do; unscoped Entra grant must be removed or scoping is void.
  https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
- Teams chat via application permissions is a **protected API** (request form, weekly review). Delegated is not.
  https://github.com/microsoft/microsoft-graph-docs-1/blob/main/concepts/teams-protected-apis.md
- To Do application permission `Tasks.Read.All` exists but is tenant-wide.
  https://learn.microsoft.com/en-us/graph/api/todo-list-lists?view=graph-rest-1.0

### 1.2 Headless flow: interactive bootstrap + cache copy

- "Microsoft recommends blocking device code flow wherever possible." Device-code RTs are protocol-tracked → `AADSTS530036` once blocked.
  https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-authentication-flows
- Managed policy "Block device code flow": created report-only, enabled ≥30d later unless set Off; needs P1/P2 or Business Premium.
  https://learn.microsoft.com/en-us/entra/identity/conditional-access/managed-policies
- `SerializableTokenCache.serialize()` is a plain string; no machine binding documented. RTs are bound to user+client, not device.
  https://learn.microsoft.com/en-us/entra/msal/python/advanced/msal-python-token-cache-serialization
  https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens
- Device binding exists only via **Token Protection** (Windows/macOS/iOS native + registered device); unsupported clients are *blocked*, not downgraded. If a tenant enables it, a Linux container cannot hold that tenant's RT at all.
  https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-token-protection
- Linux broker (WAM) is opt-in and needs Intune broker + WebKitGTK; do not enable. Bootstrap on the laptop with broker flags unset so the RT lands in the file cache, not WAM.
  https://learn.microsoft.com/en-us/entra/msal/python/advanced/linux-broker-py

Bootstrap procedure per tenant:

```
# laptop, same venv
python auth/m365_bootstrap.py org1   # acquire_token_interactive, redirect http://localhost
scp tokens/org1_cache.bin <host>:/path/to/signalslate/tokens/
```

App registration: public client, "Allow public client flows" = Yes, redirect URI `http://localhost` under "Mobile and desktop applications". Keep device code as `--device-code` fallback flag only.

### 1.3 Refresh-token lifetime

- Defaults, not configurable: MaxInactiveTime 90d; MaxAge = **until-revoked**. RT lifetime policies removed 2021-01-30.
  https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes
- RT replaces itself on every use → daily run keeps it alive indefinitely.
- Revoked by: user/admin password change or reset, admin "revoke refresh tokens", CA sign-in-frequency (periodic or "every time"), account disable, Token Protection. Password *expiry* does not revoke.
  https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens
  https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-session-lifetime
- Unverified: whether MFA re-registration revokes RTs (no doc either way).

Operational: use `acquire_token_silent_with_error` so the health check distinguishes "no cache" from "refresh rejected" and surfaces the AADSTS code on the dashboard.

### 1.4 Scopes

```python
SCOPES = ["Mail.Read", "Calendars.Read", "Tasks.Read", "Chat.Read"]
# + "ChannelMessage.Read.All" only if channel posts needed (admin consent)
```

- MSAL hardcodes `offline_access` (and openid/profile); do not pass it.
  https://learn.microsoft.com/en-us/python/api/msal/msal.application.clientapplication
- Flag: graphpermissions.merill.net lists Tasks.Read and Chat.Read delegated as admin-consent Yes; official reference says No. Official used. MSP tenant may override via consent policy regardless — see 1.6.

### 1.5 MSAL Python specifics

- msal 1.38.0 (PyPI 2026-08-24), Python ≥3.9. Current pin `1.31.0` in requirements.txt should be bumped.
  https://pypi.org/project/msal/
- msal-extensions 1.3.1: Linux uses libsecret; `build_encrypted_persistence` with explicit `fallback_to_plaintext`. No silent fallback. In a headless container use `FilePersistence` deliberately (current plain-file approach is fine; restrict volume perms).
  https://github.com/AzureAD/microsoft-authentication-extensions-for-python
- One `PublicClientApplication` per tenant with tenant-specific authority and separate cache file (current structure is correct). `/common` authority causes cache misses.
  https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant

### 1.6 One multi-tenant app vs one registration per tenant

**Recommendation: one multi-tenant public-client registration in the tenant you control**, consented in each other tenant. Fall back to per-tenant registration only where an admin refuses foreign apps.

- On first sign-in from another tenant the user consents and a service principal is created; if user consent is disabled there, admin consent is required.
  https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant
- Default: users may consent to non-admin permissions; admins can restrict to "verified publishers, low-impact" or disable. Admin-consent-request workflow exists.
  https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent
- Hand the MSP a one-click admin-consent URL:
  `https://login.microsoftonline.com/{tenant}/v2.0/adminconsent?client_id=…&scope=https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Calendars.Read https://graph.microsoft.com/Tasks.Read https://graph.microsoft.com/Chat.Read&redirect_uri=…`
  https://learn.microsoft.com/en-us/entra/identity-platform/v2-admin-consent
- No Microsoft-documented "personal automation across tenants" pattern exists; above is assembled from the multi-tenant + consent docs.

Impact on code: `.env` can collapse to one `M365_CLIENT_ID` plus `M365_ORGx_TENANT_ID` per tenant; keep per-org override for the fallback case.

---

## 2. Zoom

### 2.1 App type

**Server-to-Server OAuth.** "get your account owner access token without user interaction." Creator needs S2S-app permission plus role permissions for every admin-level scope; if role is downgraded the build flow strips admin scopes. App must be **activated** before tokens issue.
https://developers.zoom.us/docs/internal-apps/ · https://developers.zoom.us/docs/internal-apps/create/

Token: `POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=…`, TTL 3600s, no refresh token, multiple concurrent tokens allowed. Current `check_zoom()` is correct; collectors should request a fresh token per run.
https://developers.zoom.us/docs/internal-apps/s2s-oauth/

No S2S deprecation; classic scopes remain valid for existing apps; no Marketplace review for internal apps.
https://developers.zoom.us/docs/integrations/oauth-scopes/

### 2.2 Scopes (granular)

| Purpose | Endpoint | Scope | Rate class |
|---|---|---|---|
| List user's summaries (new 2026-08-17) | `GET /users/{userId}/meeting_summaries` | `meeting:read:list_summaries:admin` | MEDIUM |
| List account summaries | `GET /meetings/meeting_summaries` | `meeting:read:list_summaries:admin` | MEDIUM |
| Summary body | `GET /meetings/{uuid}/meeting_summary` | `meeting:read:summary:admin` | LIGHT |
| Past meeting details | `GET /past_meetings/{id}` | `meeting:read:past_meeting:admin` | LIGHT |
| Past participants | `GET /past_meetings/{id}/participants` | `meeting:read:list_past_participants:admin` | MEDIUM |
| Report: user's past meetings | `GET /report/users/{userId}/meetings?type=past` | `report:read:user:admin` | HEAVY |
| Transcript | `GET /meetings/{id}/transcript` | `cloud_recording:read:meeting_transcript:admin` | MEDIUM |

Source: embedded OpenAPI at https://developers.zoom.us/docs/api/meetings/ and https://developers.zoom.us/docs/api/reports/
Changelog for user-scoped summaries: https://devforum.zoom.us/t/changelog-meetings-two-new-summaries-apis-new-prevent-screen-capture-fields-in-8-endpoints-and-1-event/145655

`GET /users/{id}/meetings` returns scheduled/upcoming only, never instant meetings; do not use it for "yesterday". Reports: Pro+, ≤1 month window within last 6 months, meetings with ≥2 participants only.

### 2.3 The `:master` 403

Confirmed: `/accounts/{accountId}/…` endpoints are **Meetings Master APIs** for primary accounts managing sub-accounts; require `meeting:read:summary:master`. Single Pro/Business accounts cannot use them.
https://developers.zoom.us/docs/api/meetings/ma/

Zoom staff (2026-04-16) pointed to non-master `GET /meetings/{meetingId}/meeting_summary`.
https://devforum.zoom.us/t/cannot-retrieve-ai-companion-meeting-summary-body-via-api-missing-meetingsummary-master-scope/142967

Response: parse `summary_content` (unified Markdown). `summary_overview`, `summary_details`, `next_steps`, `edited_summary` "are deprecated and will not be supported in the future." Meeting UUIDs starting with `/` or containing `//` must be double-encoded.

Access is host-scoped: only meetings the account hosted.
https://devforum.zoom.us/t/user-level-oauth-api-access-to-ai-companion-summaries-and-transcripts-for-meetings-i-attended-but-did-not-host/143615

Flag: several 2026 threads report `meeting:read:summary:admin` missing from the S2S scope picker; resolved as insufficient role permissions on the app creator. Create the app as owner and check the picker first.
https://devforum.zoom.us/t/using-server-to-server-app-and-scopes-to-get-transcripts/144122

### 2.4 Account prerequisites

Host on Pro/Business+; "Meeting Summary with AI Companion" user setting enabled; E2EE meetings have no summaries; keep "Only share meeting summaries by email" **off**. No admin toggle named "API access to summaries" found (unverifiable). No endpoint rename; ZoomMate (June 2026) is a separate product.
https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0057960 · https://news.zoom.com/zoom-launches-zoommate/

### 2.5 Polling vs webhooks

Poll. `from`/`to` in `yyyy-MM-dd'T'HH:mm:ss'Z'`, `time_filter_field=summary_created_time` (added 2026-01-12), `page_size` ≤300, no documented max range. Use a small overlap window for late summaries. Webhook `meeting.summary_completed` exists but needs a public HTTPS endpoint; not LAN-friendly.
https://developers.zoom.us/docs/api/meetings/events/

### 2.6 Rate limits

Pro: Light 30/s, Medium 20/s, Heavy 10/s, 30k/day heavy. ~10 meetings/day is negligible; back off on 429.
https://developers.zoom.us/docs/api/rate-limits/

---

## 3. Slack (multiple workspaces)

### 3.1 Token type and scopes

**User token (xoxp).** "User tokens represent the same access a user has to a workspace"; bot tokens must be invited per channel and can't join private channels/DMs.
https://docs.slack.dev/authentication/tokens · https://docs.slack.dev/reference/methods/conversations.history

```
channels:history groups:history im:history mpim:history
channels:read   groups:read   im:read   mpim:read
users:read
# optional: reactions:read
```

Enumerate with `users.conversations` (Tier 3, membership-scoped), not `conversations.list` (Tier 2, all public).
https://docs.slack.dev/reference/methods/users.conversations

Drop `im:history`/`im:read` if the digest should not read personal DMs.

### 3.2 Expiry / rotation

User tokens do not expire. Rotation is opt-in and **irreversible**; 12h access tokens, single-use refresh tokens. **Do not enable** for a LAN-only personal tool. No 2026 mandate found.
https://docs.slack.dev/authentication/installing-with-oauth · https://docs.slack.dev/authentication/using-token-rotation

### 3.3 Search: not suitable

- `search.messages` / `search:read`: "This is a legacy method." No sunset date.
  https://docs.slack.dev/reference/methods/search.messages/
- `assistant.search.context` (Real-time Search API, GA 2026-02-17): query-driven semantic search, 20 results/page, ~10 req/min per user, requires Agents & AI Apps toggle, private/im/mpim need in-client user consent, semantic search needs Business+ with AI Search, and "You must not store or copy any of the data retrieved from this API."
  https://docs.slack.dev/apis/web-api/real-time-search-api/ · https://docs.slack.dev/reference/methods/assistant.search.context/

Neither fits "everything since 06:00 yesterday." Use `conversations.history(oldest=ts)` + `conversations.replies`.

### 3.4 Workspace admin policy

App approval is **off by default** on all plans. If enabled, member-built apps go through "request approval". Ask each admin: (a) confirm member installs allowed or approve the app with the exact user-scope list; (b) on Enterprise Grid, org-level approval.
https://slack.com/help/articles/222386767-Manage-app-approval-for-your-workspace

### 3.5 Rate limits and the non-Marketplace cap

- Tiers: T2 20+/min, T3 50+/min. `conversations.history` T3, ≤1000 msgs/req.
  https://docs.slack.dev/apis/web-api/rate-limits
- 2025-05-29 change: `conversations.history`/`replies` capped at **1 req/min, 15 objects** for apps "commercially distributed outside of the Marketplace (unlisted)". "Internal customer-built applications are not impacted." Existing unlisted installs cut over 2026-03-03.
  https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/ · https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/
- **Keep one app per workspace with distribution never enabled.** Flag: Slack does not spell out the exact "internal" classification mechanism; not enabling public distribution is the safe path.

### 3.6 2026 changes

RTS API GA (Feb 17); system notifications now from user `USLACK` (Jun 17) — filter it; Marketplace ≥10-install rule (Sep 1) irrelevant. Classic non-granular app deprecation reportedly 2026-11-16 (unverified); new apps are granular. No deprecation of `*:history` scopes.
https://docs.slack.dev/changelog/

---

## 4. reMarkable 2

### 4.1 Fork status (GitHub API, 2026-09-13)

| Repo | archived | last push | latest release |
|---|---|---|---|
| juruen/rmapi | yes | 2023-12-23 | v0.0.25 |
| **ddvk/rmapi** | **no** | **2026-08-23** | **v0.0.35 (2026-08-19)** |
| platinummonkey/rmapi | yes | 2025-12-29 | none |
| ddvk/rmfakecloud | no | 2026-07-10 | v0.0.31 |

README's fork recommendation is inverted. Supports sync15 and root-index schema v3/v4 (`RMAPI_FORCE_SCHEMA_VERSION`).
https://github.com/ddvk/rmapi · https://github.com/reHackable/awesome-reMarkable

Related: `rmitchellscott/aviary` v1.10.0 (2026-08-26), Docker HTTP/webhook uploader wrapping rmapi.
https://github.com/rmitchellscott/aviary

### 4.2 Cloud auth

- Pair: 8-char code from **https://my.remarkable.com/device/browser/connect** → `POST …/token/json/2/device/new` → device token in `$RMAPI_CONFIG` (else `~/.rmapi`, else `~/.config/rmapi/rmapi.conf`). User token auto-minted from device token.
  https://raw.githubusercontent.com/ddvk/rmapi/master/api/auth.go · https://raw.githubusercontent.com/ddvk/rmapi/master/config/config.go
- Device token: no documented expiry; community says it doesn't expire (unverified from reMarkable).
- **Since 2026-05-30 minting a user token fails with 400 unless a real tablet is already linked to the account.** Pair the RM2 first.
  https://github.com/ddvk/rmapi/issues/69
- API breaks in 2026: `.docSchema` header change (May, v0.0.34); sorted root index (Aug, v0.0.35). New `*.tectonic.remarkable.com` / `gentree/v1/` endpoints seen in xochitl 3.27.1 → future churn likely. Pin version, watch releases.
  https://github.com/ddvk/rmapi/issues/62 · https://github.com/ddvk/rmapi/issues/76 · https://github.com/ddvk/rmapi/issues/66
- Connect subscription: not required for cloud sync per third-party sources; free tier drops files unopened for 50 days (irrelevant for daily new files). reMarkable's support site blocks headless fetch — unverified.

### 4.3 Official alternatives

None. developer.remarkable.com covers on-device SDKs only. Drive/Dropbox/OneDrive integrations are manual browse-and-import from the tablet, not auto-pull.
https://developer.remarkable.com/documentation

### 4.4 Non-cloud alternatives

- USB web UI `http://10.11.99.1/upload`: needs USB tether or `webinterface-wifi` hack (wiped by firmware updates).
- SSH/scp into xochitl + restart: tablet must be awake on Wi-Fi with stable IP; fragile.
- rmfakecloud: validated through firmware 3.27.1; needs proxy + CA on tablet, reinstalled after every firmware update; loses official cloud.

**Most reliable unattended path: ddvk/rmapi against the official cloud.**

### 4.5 Python integration

PyPI rmapy (2021), rmcl (2021), remarkable-cli (2021) all pre-sync15 and dead. Shell out to the Go binary:

```python
subprocess.run(["rmapi", "put", "--force", f"{date}.pdf", "/Daily"],
               env={**os.environ, "RMAPI_CONFIG": "/app/tokens/rmapi.conf"}, check=True)
```

`put` fails on duplicate names without `--force` (replaces, drops annotations) or `--content-only` (keeps annotations). Use unique daily filenames. `RMAPI_TRACE=1` for debugging. Dockerfile in repo builds a scratch image with non-root user.

### 4.6 Firmware

3.27 rolling out since May 2026; 3.28 listed. rmapi compatibility is with the cloud, not firmware; rmfakecloud only validated to 3.27.1.
https://support.remarkable.com/s/article/Release-notes-overview

---

## Action items for the codebase

1. `pipeline/health.py`: `SCOPES += ["Chat.Read"]`; switch to `acquire_token_silent_with_error`; surface AADSTS codes.
2. New `auth/m365_bootstrap.py`: `acquire_token_interactive` with `http://localhost` redirect; keep `m365_device_auth.py` as fallback.
3. `requirements.txt`: `msal==1.38.0`.
4. `.env.example`: reconcile redirect-URI comment with README; `Tasks.Read` not `Tasks.ReadWrite`; optionally single `M365_CLIENT_ID`.
5. README §3 Zoom: scopes table above; §4 Slack: replace search paragraph with history-based scopes; §5 reMarkable: `ddvk/rmapi`, `/device/browser/connect`, link-tablet-first requirement.
6. Phase 2 Slack collector: `users.conversations` → `conversations.history(oldest)` → `conversations.replies`; filter `USLACK`.
7. Phase 2 Zoom collector: `GET /users/me/meeting_summaries?time_filter_field=summary_created_time` → `GET /meetings/{uuid}/meeting_summary`, read `summary_content`; double-encode UUIDs.
8. Phase 5: build `ddvk/rmapi` ≥v0.0.35 into the api image; mount `rmapi.conf` under `tokens/`.
