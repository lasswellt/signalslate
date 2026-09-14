"""
Unit tests for pipeline.health: .env parsing and the scope-verification paths.

_env() reads ROOT/".env" on every call, so each test points pipeline.health.ROOT at a tmp_path
holding a purpose-built .env. No network: the Slack/Zoom checks get a stubbed requests.post.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import health  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Writes a .env into tmp_path and points health.ROOT/TOKEN_DIR at it."""

    def _write(contents: str) -> Path:
        (tmp_path / ".env").write_text(contents)
        monkeypatch.setattr(health, "ROOT", tmp_path)
        monkeypatch.setattr(health, "TOKEN_DIR", tmp_path / "tokens")
        return tmp_path

    return _write


class FakeResponse:
    def __init__(self, json_data, headers=None):
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def slack_scope_header(scopes) -> dict:
    return {"x-oauth-scopes": ",".join(sorted(scopes))}


# --- .env parsing -----------------------------------------------------------------


def test_m365_aliases_sorted_numerically_not_lexically(env):
    env("M365_ORG1_ALIAS=one\nM365_ORG2_ALIAS=two\nM365_ORG10_ALIAS=ten\n")
    assert health.m365_aliases() == ["one", "two", "ten"]


def test_m365_aliases_skips_blank_values(env):
    env("M365_ORG1_ALIAS=one\nM365_ORG2_ALIAS=\nM365_ORG3_ALIAS=three\n")
    assert health.m365_aliases() == ["one", "three"]


def test_m365_tenant_config_falls_back_to_shared_client_id(env):
    env("M365_CLIENT_ID=shared\nM365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\n")
    assert health.m365_tenant_config("work") == {
        "alias": "work",
        "tenant_id": "tid",
        "client_id": "shared",
    }


def test_m365_tenant_config_per_tenant_client_id_overrides(env):
    env(
        "M365_CLIENT_ID=shared\n"
        "M365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\nM365_ORG1_CLIENT_ID=own\n"
    )
    assert health.m365_tenant_config("work")["client_id"] == "own"


def test_m365_tenant_config_none_without_tenant_id(env):
    env("M365_CLIENT_ID=shared\nM365_ORG1_ALIAS=work\n")
    assert health.m365_tenant_config("work") is None


def test_m365_tenant_config_none_for_unknown_alias(env):
    env("M365_CLIENT_ID=shared\nM365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\n")
    assert health.m365_tenant_config("nope") is None


def test_slack_workspaces_lowercases_labels_and_sorts(env):
    env("SLACK_WORK_TOKEN=xoxp-1\nSLACK_ACME_TOKEN=xoxp-2\n")
    assert health.slack_workspaces() == {"acme": "xoxp-2", "work": "xoxp-1"}


def test_slack_workspaces_keeps_empty_token_as_none(env):
    env("SLACK_WORK_TOKEN=\n")
    assert health.slack_workspaces() == {"work": None}


def test_known_sources_lists_every_declared_source(env):
    env("M365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\nSLACK_ACME_TOKEN=xoxp-1\n")
    assert health.known_sources() == ["m365_work", "zoom", "slack_acme"]


# --- Slack scope verification -----------------------------------------------------


def test_check_slack_ok_with_every_required_scope(env, monkeypatch):
    env("")
    monkeypatch.setattr(
        health.requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"ok": True, "user": "tom", "team": "acme"},
            slack_scope_header(health.SLACK_SCOPES),
        ),
    )
    result = health.check_slack("acme", "xoxp-1")
    assert result.status == "ok"
    assert "team=acme" in result.detail


def test_check_slack_errors_on_missing_scope(env, monkeypatch):
    env("")
    granted = health.SLACK_SCOPES - {"groups:read", "groups:history"}
    monkeypatch.setattr(
        health.requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"ok": True, "user": "tom", "team": "acme"}, slack_scope_header(granted)
        ),
    )
    result = health.check_slack("acme", "xoxp-1")
    assert result.status == "error"
    assert "missing scopes: groups:history, groups:read" in result.detail


def test_check_slack_dm_scopes_optional_when_skip_dms_set(env, monkeypatch):
    env("SLACK_SKIP_DMS=1\n")
    granted = health.SLACK_SCOPES - health.SLACK_DM_SCOPES
    monkeypatch.setattr(
        health.requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"ok": True, "user": "tom", "team": "acme"}, slack_scope_header(granted)
        ),
    )
    assert health.check_slack("acme", "xoxp-1").status == "ok"


def test_check_slack_dm_scopes_required_by_default(env, monkeypatch):
    env("")
    granted = health.SLACK_SCOPES - health.SLACK_DM_SCOPES
    monkeypatch.setattr(
        health.requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"ok": True, "user": "tom", "team": "acme"}, slack_scope_header(granted)
        ),
    )
    assert health.check_slack("acme", "xoxp-1").status == "error"


def test_check_slack_api_error_reported_before_scopes(env, monkeypatch):
    env("")
    monkeypatch.setattr(
        health.requests, "post", lambda *a, **k: FakeResponse({"ok": False, "error": "invalid_auth"})
    )
    result = health.check_slack("acme", "xoxp-1")
    assert result.status == "error"
    assert result.detail == "invalid_auth"


def test_check_slack_without_token_does_not_call_out(env):
    env("")
    assert health.check_slack("acme", None).status == "error"


def test_check_slack_network_failure(env, monkeypatch):
    env("")

    def boom(*a, **k):
        raise health.requests.RequestException("connection refused")

    monkeypatch.setattr(health.requests, "post", boom)
    result = health.check_slack("acme", "xoxp-1")
    assert result.status == "error"
    assert "connection refused" in result.detail


# --- Zoom scope verification ------------------------------------------------------


ZOOM_ENV = "ZOOM_ACCOUNT_ID=a\nZOOM_CLIENT_ID=b\nZOOM_CLIENT_SECRET=c\n"


def test_check_zoom_ok_with_every_required_scope(env, monkeypatch):
    env(ZOOM_ENV)
    monkeypatch.setattr(
        health.requests,
        "post",
        lambda *a, **k: FakeResponse(
            {"expires_in": 3600, "scope": " ".join(sorted(health.ZOOM_SCOPES))}
        ),
    )
    assert health.check_zoom().status == "ok"


def test_check_zoom_errors_on_missing_admin_scope(env, monkeypatch):
    env(ZOOM_ENV)
    granted = health.ZOOM_SCOPES - {"meeting:read:summary:admin"}
    monkeypatch.setattr(
        health.requests,
        "post",
        lambda *a, **k: FakeResponse({"expires_in": 3600, "scope": " ".join(sorted(granted))}),
    )
    result = health.check_zoom()
    assert result.status == "error"
    assert "missing scopes: meeting:read:summary:admin" in result.detail


def test_check_zoom_missing_credentials(env):
    env("ZOOM_ACCOUNT_ID=a\n")
    assert health.check_zoom().status == "error"


# --- active-source toggles --------------------------------------------------------


def test_check_all_configured_skips_inactive_sources(env, monkeypatch):
    env("M365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\nSLACK_ACME_TOKEN=xoxp-1\n")
    monkeypatch.setattr(health, "check_m365", lambda alias: health.HealthResult(f"m365_{alias}", "ok", ""))
    monkeypatch.setattr(health, "check_zoom", lambda: health.HealthResult("zoom", "ok", ""))
    monkeypatch.setattr(health, "check_slack", lambda l, t: health.HealthResult(f"slack_{l}", "ok", ""))

    results = health.check_all_configured({"m365_work": True, "zoom": False, "slack_acme": True})
    assert [r.source for r in results] == ["m365_work", "slack_acme"]


def test_check_all_configured_empty_when_nothing_active(env):
    env("M365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\n")
    assert health.check_all_configured({}) == []


# --- environment reading (the container has no .env file) -----------------------------


def test_env_reads_process_environment_when_no_dotenv(monkeypatch, tmp_path):
    """
    The deployed container has no .env: the Dockerfile doesn't copy it and compose's env_file
    injects it into the environment. Reading only the file made every source invisible in Docker.
    """
    monkeypatch.setattr(health, "ROOT", tmp_path)  # no .env on disk
    monkeypatch.setenv("M365_ORG1_ALIAS", "work")
    monkeypatch.setenv("M365_ORG1_TENANT_ID", "tid")
    monkeypatch.setenv("M365_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_ACME_TOKEN", "xoxp-1")

    assert health.m365_aliases() == ["work"]
    assert health.slack_workspaces() == {"acme": "xoxp-1"}
    assert health.m365_tenant_config("work")["client_id"] == "cid"


def test_process_environment_wins_over_dotenv(env, monkeypatch):
    env("M365_ORG1_ALIAS=from-file\nM365_ORG1_TENANT_ID=tid\nM365_CLIENT_ID=cid\n")
    monkeypatch.setenv("M365_ORG1_ALIAS", "from-env")
    assert health.m365_aliases() == ["from-env"]


def test_env_flag_parses_rather_than_testing_truthiness(env, monkeypatch):
    env("")
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SLACK_SKIP_DMS", value)
        assert health.env_flag("SLACK_SKIP_DMS") is True
    for value in ("0", "false", "False", "no", "off", ""):
        monkeypatch.setenv("SLACK_SKIP_DMS", value)
        assert health.env_flag("SLACK_SKIP_DMS") is False


def test_env_flag_false_when_unset(env, monkeypatch):
    env("")
    monkeypatch.delenv("SLACK_SKIP_DMS", raising=False)
    assert health.env_flag("SLACK_SKIP_DMS") is False
