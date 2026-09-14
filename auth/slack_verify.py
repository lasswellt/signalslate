"""Thin CLI wrapper around pipeline.health.check_slack for every SLACK_<LABEL>_TOKEN in .env."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.health import check_slack, slack_workspaces  # noqa: E402

if __name__ == "__main__":
    workspaces = slack_workspaces()
    if not workspaces:
        sys.exit("No SLACK_<LABEL>_TOKEN entries in .env")
    results = {label: check_slack(label, token) for label, token in workspaces.items()}
    for label, result in results.items():
        print(f"[{label}] {result.status.upper()} — {result.detail}")
    sys.exit(1 if any(r.status == "error" for r in results.values()) else 0)
