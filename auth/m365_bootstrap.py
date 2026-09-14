"""
One-time interactive sign-in (auth-code + PKCE via the system browser) for one M365 tenant alias.
Run this on a laptop, then copy tokens/<alias>_cache.bin to the server's tokens/ volume.

The cache is bound to user+client, not to this machine, so it works from the Docker container.
After that, pipeline/health.py's daily silent refresh keeps the refresh token alive (it's
until-revoked with a 90-day inactivity limit, and every use issues a fresh one).

Re-run when the dashboard shows an AADSTS error for this org (password change, admin revoke,
sign-in-frequency policy).

Usage:
    python auth/m365_bootstrap.py org1
    python auth/m365_bootstrap.py org1 --force   # re-auth even if a cache exists
"""
import argparse
import sys
from pathlib import Path

import msal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.health import SCOPES, TOKEN_DIR, check_m365, m365_app, m365_cache_path, m365_tenant_config  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive (browser) sign-in for one M365 tenant alias")
    parser.add_argument("alias", help="e.g. org1, org2, org3 — must match M365_ORGx_ALIAS in .env")
    parser.add_argument("--force", action="store_true", help="re-authenticate even if a cache exists")
    args = parser.parse_args()

    TOKEN_DIR.mkdir(exist_ok=True)
    cache_path = m365_cache_path(args.alias)

    if cache_path.exists() and not args.force:
        result = check_m365(args.alias)
        print(f"[{args.alias}] {result.status.upper()} — {result.detail}")
        if result.status == "ok":
            sys.exit(0)
        print(f"[{args.alias}] silent refresh failed; re-authenticating interactively")

    cfg = m365_tenant_config(args.alias)
    if cfg is None:
        sys.exit(f"No usable .env entry for alias={args.alias!r} (need M365_ORGx_TENANT_ID and M365_CLIENT_ID or M365_ORGx_CLIENT_ID)")

    cache = msal.SerializableTokenCache()
    # Broker (WAM) stays off — with it enabled the refresh token lands in the OS broker, not this
    # file, and becomes device-bound; neither is what a headless container wants.
    app = m365_app(cfg, cache)
    print(f"[{args.alias}] opening browser for tenant {cfg['tenant_id']} ...")
    result = app.acquire_token_interactive(
        scopes=SCOPES,
        prompt="select_account",
        timeout=300,
        # Redirect http://localhost must be registered under "Mobile and desktop applications".
    )

    if "access_token" not in result:
        sys.exit(f"[{args.alias}] Auth failed: {result.get('error')}: {result.get('error_description')}")

    cache_path.write_text(cache.serialize())
    acct = result.get("id_token_claims", {}).get("preferred_username", "?")
    print(f"[{args.alias}] OK — signed in as {acct}, expires_in={result['expires_in']}s")
    print(f"[{args.alias}] cache written to {cache_path}")
    print(f"[{args.alias}] copy it to the server's tokens/ directory, e.g.:")
    print(f"    scp {cache_path} <host>:/path/to/signalslate/tokens/")
