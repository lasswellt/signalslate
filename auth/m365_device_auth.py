"""
FALLBACK ONLY — device-code approval for one M365 tenant alias.

Prefer auth/m365_bootstrap.py. Microsoft ships a managed Conditional Access policy that blocks
device code flow, and a refresh token minted this way is "protocol tracked": it dies with
AADSTS530036 once that policy turns on, even though the daily silent refresh never uses device
code. Use this only for a tenant that explicitly permits device code and can't do the
interactive flow.

Usage:
    python auth/m365_device_auth.py org1
"""
import argparse
import sys
from pathlib import Path

import msal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.health import SCOPES, TOKEN_DIR, check_m365, m365_app, m365_cache_path, m365_tenant_config  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fallback device-code approval for one M365 tenant alias")
    parser.add_argument("alias", help="e.g. org1, org2, org3 — must match M365_ORGx_ALIAS in .env")
    args = parser.parse_args()

    TOKEN_DIR.mkdir(exist_ok=True)
    cache_path = m365_cache_path(args.alias)

    if cache_path.exists():
        result = check_m365(args.alias)
        print(f"[{args.alias}] {result.status.upper()} — {result.detail}")
        sys.exit(0 if result.status == "ok" else 1)

    cfg = m365_tenant_config(args.alias)
    if cfg is None:
        sys.exit(f"No usable .env entry for alias={args.alias!r} (need M365_ORGx_TENANT_ID and M365_CLIENT_ID or M365_ORGx_CLIENT_ID)")

    cache = msal.SerializableTokenCache()
    app = m365_app(cfg, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        sys.exit(f"Failed to create device flow: {flow}")
    print(f"\n[{args.alias}] {flow['message']}\n")
    result = app.acquire_token_by_device_flow(flow)  # blocks until approved or expired
    cache_path.write_text(cache.serialize())

    if "access_token" not in result:
        sys.exit(f"[{args.alias}] Auth failed: {result.get('error_description')}")
    print(f"[{args.alias}] OK — token acquired, expires_in={result['expires_in']}s")
