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
    def __init__(self, json_data, headers=None, status_code=200):
        self._json = json_data
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise health.requests.HTTPError(f"{self.status_code} Server Error")


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


# --- Gmail auth -------------------------------------------------------------------

GMAIL_ENV = "GMAIL_CLIENT_ID=cid\nGMAIL_CLIENT_SECRET=csecret\nGMAIL_PERSONAL_REFRESH_TOKEN=rt-1\n"


def gmail_stub(monkeypatch, token_resp, profile_resp=None):
    """Stubs the token POST and profile GET; returns the recorded calls."""
    calls = {"post": [], "get": []}

    def post(url, **kwargs):
        calls["post"].append((url, kwargs))
        return token_resp

    def get(url, **kwargs):
        calls["get"].append((url, kwargs))
        return profile_resp

    monkeypatch.setattr(health.requests, "post", post)
    monkeypatch.setattr(health.requests, "get", get)
    return calls


def test_gmail_accounts_lowercases_labels_and_sorts(env):
    env("GMAIL_WORK_REFRESH_TOKEN=b\nGMAIL_PERSONAL_REFRESH_TOKEN=a\n")
    assert list(health.gmail_accounts()) == ["personal", "work"]


def test_gmail_accounts_keeps_empty_token_as_none(env):
    env("GMAIL_WORK_REFRESH_TOKEN=\n")
    assert health.gmail_accounts() == {"work": None}


def test_gmail_accounts_ignores_shared_client_credentials(env):
    env(GMAIL_ENV)
    assert list(health.gmail_accounts()) == ["personal"]


def test_known_sources_lists_gmail_after_slack(env):
    env("SLACK_ACME_TOKEN=xoxp-1\nGMAIL_PERSONAL_REFRESH_TOKEN=rt\n")
    assert health.known_sources() == ["zoom", "slack_acme", "gmail_personal"]


def test_gmail_token_response_posts_refresh_grant_and_returns_raw_json(env, monkeypatch):
    env(GMAIL_ENV)
    body = {"access_token": "at", "expires_in": 3599, "scope": health.GMAIL_SCOPE}
    calls = gmail_stub(monkeypatch, FakeResponse(body))
    assert health.gmail_token_response("personal") == body
    url, kwargs = calls["post"][0]
    assert url == "https://oauth2.googleapis.com/token"
    assert kwargs["data"] == {
        "client_id": "cid",
        "client_secret": "csecret",
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
    }


def test_gmail_token_response_missing_client_credentials(env):
    env("GMAIL_PERSONAL_REFRESH_TOKEN=rt-1\n")
    with pytest.raises(RuntimeError, match="GMAIL_CLIENT_ID"):
        health.gmail_token_response("personal")


def test_gmail_token_response_unknown_label(env):
    env(GMAIL_ENV)
    with pytest.raises(RuntimeError, match="GMAIL_OTHER_REFRESH_TOKEN"):
        health.gmail_token_response("other")


def test_gmail_token_response_invalid_grant_raises_gmail_auth_error(env, monkeypatch):
    env(GMAIL_ENV)
    gmail_stub(monkeypatch, FakeResponse({"error": "invalid_grant"}, status_code=400))
    with pytest.raises(health.GmailAuthError):
        health.gmail_token_response("personal")


def test_gmail_token_response_other_400_is_not_an_auth_error(env, monkeypatch):
    env(GMAIL_ENV)
    gmail_stub(monkeypatch, FakeResponse({"error": "invalid_client"}, status_code=400))
    with pytest.raises(health.requests.HTTPError):
        health.gmail_token_response("personal")


def test_check_gmail_ok_with_scope_and_live_profile(env, monkeypatch):
    env(GMAIL_ENV)
    calls = gmail_stub(
        monkeypatch,
        FakeResponse({"access_token": "at", "scope": f"openid {health.GMAIL_SCOPE}"}),
        FakeResponse({"emailAddress": "me@example.com"}),
    )
    result = health.check_gmail("personal")
    assert result.status == "ok"
    assert result.source == "gmail_personal"
    assert "me@example.com" in result.detail
    url, kwargs = calls["get"][0]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert kwargs["headers"] == {"Authorization": "Bearer at"}


def test_check_gmail_errors_on_missing_scope_without_probing(env, monkeypatch):
    env(GMAIL_ENV)
    calls = gmail_stub(
        monkeypatch,
        FakeResponse({"access_token": "at", "scope": "https://www.googleapis.com/auth/gmail.labels"}),
    )
    result = health.check_gmail("personal")
    assert result.status == "error"
    assert "missing scope" in result.detail
    assert calls["get"] == []


def test_check_gmail_invalid_grant_names_causes_and_bootstrap(env, monkeypatch):
    env(GMAIL_ENV)
    gmail_stub(monkeypatch, FakeResponse({"error": "invalid_grant"}, status_code=400))
    result = health.check_gmail("personal")
    assert result.status == "error"
    assert "Testing" in result.detail
    assert "password" in result.detail
    assert "revoked" in result.detail
    assert "auth/gmail_bootstrap.py personal" in result.detail


def test_check_gmail_token_endpoint_5xx_is_transient_not_reauth(env, monkeypatch):
    env(GMAIL_ENV)
    gmail_stub(monkeypatch, FakeResponse({}, status_code=503))
    result = health.check_gmail("personal")
    assert result.status == "error"
    assert "503" in result.detail
    assert "gmail_bootstrap" not in result.detail


def test_check_gmail_profile_failure_reported_with_http_detail(env, monkeypatch):
    env(GMAIL_ENV)
    gmail_stub(
        monkeypatch,
        FakeResponse({"access_token": "at", "scope": health.GMAIL_SCOPE}),
        FakeResponse({}, status_code=500),
    )
    result = health.check_gmail("personal")
    assert result.status == "error"
    assert "500" in result.detail
    assert "gmail_bootstrap" not in result.detail


def test_check_gmail_network_failure(env, monkeypatch):
    env(GMAIL_ENV)

    def boom(*a, **k):
        raise health.requests.ConnectionError("dns down")

    monkeypatch.setattr(health.requests, "post", boom)
    result = health.check_gmail("personal")
    assert result.status == "error"
    assert "dns down" in result.detail


def test_check_gmail_missing_credentials(env):
    env("GMAIL_PERSONAL_REFRESH_TOKEN=rt-1\n")
    assert health.check_gmail("personal").status == "error"


# --- active-source toggles --------------------------------------------------------


def test_check_all_configured_skips_inactive_sources(env, monkeypatch):
    env("M365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\nSLACK_ACME_TOKEN=xoxp-1\n")
    monkeypatch.setattr(health, "check_m365", lambda alias: health.HealthResult(f"m365_{alias}", "ok", ""))
    monkeypatch.setattr(health, "check_zoom", lambda: health.HealthResult("zoom", "ok", ""))
    monkeypatch.setattr(health, "check_slack", lambda l, t: health.HealthResult(f"slack_{l}", "ok", ""))

    results = health.check_all_configured({"m365_work": True, "zoom": False, "slack_acme": True})
    assert [r.source for r in results] == ["m365_work", "slack_acme"]


def test_check_all_configured_runs_active_gmail_only(env, monkeypatch):
    env("GMAIL_PERSONAL_REFRESH_TOKEN=a\nGMAIL_WORK_REFRESH_TOKEN=b\n")
    monkeypatch.setattr(health, "check_gmail", lambda l: health.HealthResult(f"gmail_{l}", "ok", ""))
    results = health.check_all_configured({"gmail_work": True, "gmail_personal": False})
    assert [r.source for r in results] == ["gmail_work"]


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


# --- LLM settings -----------------------------------------------------------------


def test_llm_settings_defaults_when_unset(env):
    env("")
    assert health.llm_settings() == {"api_key": None, "map_model": health.DEFAULT_MAP_MODEL}
    assert health.DEFAULT_MAP_MODEL == "claude-haiku-4-5-20251001"


def test_llm_settings_read_from_dotenv(env):
    env("ANTHROPIC_API_KEY=sk-test-dotenv\nSIGNALSLATE_MAP_MODEL=model-from-dotenv\n")
    assert health.llm_settings() == {"api_key": "sk-test-dotenv", "map_model": "model-from-dotenv"}


def test_llm_settings_environment_takes_precedence_over_dotenv(env, monkeypatch):
    env("ANTHROPIC_API_KEY=sk-test-dotenv\nSIGNALSLATE_MAP_MODEL=model-from-dotenv\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-environ")
    monkeypatch.setenv("SIGNALSLATE_MAP_MODEL", "model-from-environ")
    assert health.llm_settings() == {"api_key": "sk-test-environ", "map_model": "model-from-environ"}


def test_llm_settings_read_from_environment_without_dotenv_entry(env, monkeypatch):
    env("")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-environ")
    assert health.llm_settings() == {"api_key": "sk-test-environ", "map_model": health.DEFAULT_MAP_MODEL}


def test_llm_settings_blank_values_count_as_unset(env, monkeypatch):
    env("ANTHROPIC_API_KEY=\nSIGNALSLATE_MAP_MODEL=   \n")
    assert health.llm_settings() == {"api_key": None, "map_model": health.DEFAULT_MAP_MODEL}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    monkeypatch.setenv("SIGNALSLATE_MAP_MODEL", "")
    assert health.llm_settings() == {"api_key": None, "map_model": health.DEFAULT_MAP_MODEL}


def test_llm_keys_are_in_single_keys():
    # Without this the container's environment value is never merged into _env().
    assert {"ANTHROPIC_API_KEY", "SIGNALSLATE_MAP_MODEL"} <= health._SINGLE_KEYS
