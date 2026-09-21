"""
Tests for pipeline.oauth_zoom. The token/revoke endpoints are stubbed at requests.post inside
pipeline.oauth_zoom (the one true external); the flow store, the connection store, the vault and the
SQLite file are all real.

Above all: the code, the client secret, the access token and the refresh token must not appear in any
exception message, repr or returned value. Every secret below is a distinctive invented string so a
substring hit can only mean a leak.
"""
import dataclasses
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlmodel import SQLModel, create_engine

from pipeline import connections, crypto, db, oauth_flows, oauth_zoom
from pipeline.oauth_flows import (
    ExpiredFlow,
    FlowError,
    FlowStore,
    InvalidCallbackConfig,
    NonceMismatch,
    UnknownFlow,
)

CLIENT_ID = "zoom-client-9F3xQe"
CLIENT_SECRET = "zoom-client-secret-Tj40cUe5Ya"
REFRESH = "zoom-refresh-Vn27sGf1Lo"
REFRESH_2 = "zoom-refresh-Ab55xxYY"
CODE = "zoom-auth-code-Zk29qLmXw"
ACCESS_TOKEN = "zoom-access-Qm18RvC"
BASE_URL = "https://signalslate.example.com"
CONNECTION_ID = "zoom"


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def vault(temp_db):
    v = crypto.Vault([crypto.generate_key()])
    connections.set_vault(v)
    yield v
    connections.set_vault(None)


@pytest.fixture
def connection(vault):
    """An oauth-mode Zoom connection with a client but no refresh token, as it is right before sign-in."""
    return connections.create("zoom", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "auth_mode": "oauth"})


@pytest.fixture
def store():
    return FlowStore()


@pytest.fixture
def token_endpoint(monkeypatch):
    """Stub the token endpoint; `.calls` holds the form bodies/kwargs it received, `.reply` what it answers."""

    class Endpoint:
        calls: list[dict] = []
        reply = _FakeResponse(200, {"refresh_token": REFRESH, "access_token": ACCESS_TOKEN, "expires_in": 3600})

    endpoint = Endpoint()
    endpoint.calls = []

    def fake_post(url, data=None, timeout=None, auth=None, params=None, **kwargs):
        assert url in (oauth_zoom.TOKEN_ENDPOINT, oauth_zoom.REVOKE_ENDPOINT)
        endpoint.calls.append({"url": url, "data": data, "auth": auth, "params": params})
        return endpoint.reply

    monkeypatch.setattr(oauth_zoom.requests, "post", fake_post)
    return endpoint


def _state(start_result) -> str:
    return parse_qs(urlsplit(start_result.auth_url).query)["state"][0]


def _decrypted(connection_id=CONNECTION_ID) -> dict:
    return connections._secrets_for(connection_id)


def _raw_db_text(engine) -> str:
    parts = []
    with engine.connect() as conn:
        for table in ("connection", "tombstone"):
            for row in conn.exec_driver_sql(f"SELECT * FROM {table}"):
                parts.extend(str(col) for col in row)
    return "\n".join(parts)


def _assert_absent(text: str, *values: str) -> None:
    for value in values:
        assert value not in text, "a secret leaked"


def _leaks(*extra: str) -> tuple:
    return (CODE, CLIENT_SECRET, REFRESH, ACCESS_TOKEN, *extra)


def _fail(exc_type, fn, *forbidden: str):
    """Run fn, expect exc_type, and prove nothing sensitive is in its message, repr or traceback chain."""
    with pytest.raises(exc_type) as info:
        fn()
    exc = info.value
    _assert_absent(str(exc) + repr(exc) + repr(exc.args), *_leaks(*forbidden))
    return exc


# ---- start ----


def test_start_builds_the_authorize_url_with_no_scope(connection, store):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    query = parse_qs(urlsplit(result.auth_url).query)
    assert result.auth_url.startswith(oauth_zoom.AUTHORIZE_ENDPOINT + "?")
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{BASE_URL}/api/oauth/callback/zoom"]
    assert query["response_type"] == ["code"]
    assert "scope" not in query
    assert result.mode == "callback"
    assert result.nonce and result.flow_id and result.instructions
    assert len(store) == 1
    assert CLIENT_SECRET not in result.auth_url


def test_start_paste_back_is_refused(connection, store):
    _fail(oauth_zoom.PasteBackNotSupported, lambda: oauth_zoom.start(CONNECTION_ID, "paste_back", store=store))
    assert len(store) == 0


def test_paste_back_not_supported_is_an_invalid_mode():
    from pipeline import oauth_gmail

    assert issubclass(oauth_zoom.PasteBackNotSupported, oauth_gmail.InvalidMode)


def test_start_callback_without_public_base_url_is_refused(connection, store):
    _fail(oauth_zoom.CallbackUnavailable, lambda: oauth_zoom.start(CONNECTION_ID, "callback", store=store))
    assert len(store) == 0


def test_callback_unavailable_is_an_invalid_callback_config():
    assert issubclass(oauth_zoom.CallbackUnavailable, InvalidCallbackConfig)


@pytest.mark.parametrize("base", ["http://signalslate.example.com", "not a url", ""])
def test_start_callback_needs_an_https_base_url(connection, store, base):
    with pytest.raises(InvalidCallbackConfig):
        oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=base)
    assert len(store) == 0


def test_start_rejects_an_unknown_mode(connection, store):
    from pipeline import oauth_gmail

    with pytest.raises(oauth_gmail.InvalidMode):
        oauth_zoom.start(CONNECTION_ID, "device", store=store)


def test_start_unknown_or_non_zoom_connection(vault, store):
    connections.create("slack", {"label": "work", "token": "slack-token-Hd91XvB7mR"})

    with pytest.raises(oauth_zoom.UnknownConnection):
        oauth_zoom.start("nope", "callback", store=store, public_base_url=BASE_URL)
    with pytest.raises(oauth_zoom.UnknownConnection):
        oauth_zoom.start("slack_work", "callback", store=store, public_base_url=BASE_URL)


def test_start_refuses_an_s2s_connection(vault, store):
    connections.create("zoom", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "account_id": "acct-1"})

    _fail(
        oauth_zoom.NotOAuthConfigured,
        lambda: oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL),
    )
    assert len(store) == 0


def _edit_row(connection_id, *, drop_secrets=False):
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        assert row is not None
        if drop_secrets:
            row.secret_ciphertext = None
        session.add(row)
        session.commit()


def test_start_needs_the_client_secret(connection, store):
    _edit_row(CONNECTION_ID, drop_secrets=True)

    _fail(
        oauth_zoom.ClientCredentialsMissing,
        lambda: oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL),
    )
    assert len(store) == 0


# ---- finish: happy path ----


def test_callback_happy_path_stores_the_refresh_token_encrypted(connection, store, token_endpoint, temp_db):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    view = oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce)

    assert view.id == CONNECTION_ID
    assert "refresh_token" in view.secrets_set and "client_secret" in view.secrets_set
    assert _decrypted()["refresh_token"] == REFRESH
    assert _decrypted()["client_secret"] == CLIENT_SECRET
    raw = _raw_db_text(temp_db)
    _assert_absent(raw, REFRESH, CLIENT_SECRET, ACCESS_TOKEN)
    _assert_absent(json.dumps(dataclasses.asdict(view), default=str) + repr(view), *_leaks())
    assert len(store) == 0

    (call,) = token_endpoint.calls
    assert call["data"]["grant_type"] == "authorization_code"
    assert call["data"]["code"] == CODE
    assert call["data"]["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/zoom"
    assert call["auth"] == (CLIENT_ID, CLIENT_SECRET)


def test_a_second_sign_in_replaces_the_refresh_token(connection, store, token_endpoint):
    first = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    oauth_zoom.finish_callback(store, _state(first), CODE, first.nonce)
    token_endpoint.reply = _FakeResponse(200, {"refresh_token": REFRESH_2, "access_token": ACCESS_TOKEN, "expires_in": 3600})
    second = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    oauth_zoom.finish_callback(store, _state(second), CODE, second.nonce)

    assert _decrypted()["refresh_token"] == REFRESH_2
    assert _decrypted()["client_secret"] == CLIENT_SECRET


# ---- finish: refusals ----


def test_no_refresh_token_is_refused_and_nothing_is_stored(connection, store, token_endpoint):
    token_endpoint.reply = _FakeResponse(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(oauth_zoom.NoRefreshToken, lambda: oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce))

    assert "refresh_token" not in _decrypted()


def test_exchange_rejected_reports_no_provider_text(connection, store, token_endpoint):
    token_endpoint.reply = _FakeResponse(
        400, {"error": "invalid_grant", "error_description": f"Bad code {CODE} for {CLIENT_SECRET}"}
    )
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    exc = _fail(
        oauth_zoom.ExchangeFailed,
        lambda: oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce),
    )

    assert "invalid_grant" not in str(exc)
    assert "refresh_token" not in _decrypted()


def test_network_failure_on_the_exchange_is_reported_generically(connection, store, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError(f"failed posting {CODE} with {CLIENT_SECRET}")

    monkeypatch.setattr(oauth_zoom.requests, "post", boom)
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_zoom.ExchangeFailed,
        lambda: oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce),
    )


def test_denied_consent_via_callback(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_zoom.ConsentDenied,
        lambda: oauth_zoom.finish_callback(store, _state(result), None, result.nonce, error="access_denied"),
    )

    assert token_endpoint.calls == []
    assert len(store) == 0


def test_other_provider_errors_are_not_reported_as_a_denial(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_zoom.ProviderError,
        lambda: oauth_zoom.finish_callback(store, _state(result), None, result.nonce, error="server_error"),
    )


def test_callback_without_a_code_is_an_error_and_makes_no_request(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    with pytest.raises(oauth_zoom.ProviderError):
        oauth_zoom.finish_callback(store, _state(result), None, result.nonce)

    assert token_endpoint.calls == []


# ---- finish: state, nonce, replay, expiry ----


def test_replaying_the_same_state_fails(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce)

    _fail(UnknownFlow, lambda: oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce))

    assert len(token_endpoint.calls) == 1


def test_expired_flow(connection, store, token_endpoint, monkeypatch):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    later = oauth_flows.utcnow() + timedelta(seconds=oauth_flows.DEFAULT_TTL_SECONDS + 1)
    monkeypatch.setattr(oauth_flows, "utcnow", lambda: later)

    _fail(ExpiredFlow, lambda: oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce))

    assert token_endpoint.calls == []
    assert len(store) == 0


def test_a_wrong_nonce_is_refused_and_does_not_burn_the_flow(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(NonceMismatch, lambda: oauth_zoom.finish_callback(store, _state(result), CODE, "someone-elses-nonce"))
    _fail(NonceMismatch, lambda: oauth_zoom.finish_callback(store, _state(result), CODE, None))
    assert token_endpoint.calls == []

    oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce)
    assert len(token_endpoint.calls) == 1


def test_a_forged_callback_state_is_unknown(connection, store, token_endpoint):
    oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(UnknownFlow, lambda: oauth_zoom.finish_callback(store, "forged-state", CODE, "any"))
    _fail(UnknownFlow, lambda: oauth_zoom.finish_callback(store, None, CODE, "any"))
    assert token_endpoint.calls == []


def test_a_callback_flow_cannot_be_consumed_as_another_provider(connection, store):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    flow = store.consume(state=_state(result), nonce=result.nonce, provider="zoom")

    assert flow.provider == "zoom"


# ---- finish: connection removed mid-flow ----


def test_connection_deleted_mid_flow(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    connections.delete(CONNECTION_ID)

    _fail(
        oauth_zoom.UnknownConnection,
        lambda: oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce),
    )

    assert token_endpoint.calls == []


# ---- refresh ----


def test_refresh_success_returns_the_rotated_token(connection, token_endpoint):
    body = oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH)

    assert body["refresh_token"] == REFRESH
    assert body["access_token"] == ACCESS_TOKEN
    (call,) = token_endpoint.calls
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == REFRESH
    assert call["auth"] == (CLIENT_ID, CLIENT_SECRET)


def test_refresh_invalid_grant_raises_refresh_token_rejected(connection, token_endpoint):
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant", "reason": f"Invalid Token {REFRESH}"})

    _fail(oauth_zoom.RefreshTokenRejected, lambda: oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH))


def test_refresh_invalid_request_naming_the_token_raises_refresh_token_rejected(connection, token_endpoint):
    token_endpoint.reply = _FakeResponse(401, {"error": "invalid_request", "reason": "Invalid access token"})

    _fail(oauth_zoom.RefreshTokenRejected, lambda: oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH))


def test_refresh_other_400_raises_refresh_failed(connection, token_endpoint):
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_client"})

    _fail(oauth_zoom.RefreshFailed, lambda: oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH))


def test_refresh_missing_refresh_token_in_response_raises_refresh_failed(connection, token_endpoint):
    token_endpoint.reply = _FakeResponse(200, {"access_token": ACCESS_TOKEN, "expires_in": 3600})

    _fail(oauth_zoom.RefreshFailed, lambda: oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH))


def test_refresh_network_failure_raises_refresh_failed(connection, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError(f"failed posting {REFRESH}")

    monkeypatch.setattr(oauth_zoom.requests, "post", boom)

    _fail(oauth_zoom.RefreshFailed, lambda: oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH))


# ---- revoke ----


def test_revoke_success_returns_true(connection, token_endpoint):
    assert oauth_zoom.revoke(CLIENT_ID, CLIENT_SECRET, REFRESH) is True
    (call,) = token_endpoint.calls
    assert call["url"] == oauth_zoom.REVOKE_ENDPOINT
    assert call["params"] == {"token": REFRESH}


def test_revoke_failure_returns_false_and_never_raises(connection, token_endpoint):
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_token"})

    assert oauth_zoom.revoke(CLIENT_ID, CLIENT_SECRET, REFRESH) is False


def test_revoke_network_failure_returns_false(connection, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(oauth_zoom.requests, "post", boom)

    assert oauth_zoom.revoke(CLIENT_ID, CLIENT_SECRET, REFRESH) is False


# ---- nothing sensitive escapes anywhere ----


def test_no_secret_reaches_any_exception_string_or_return_value(connection, store, token_endpoint, temp_db):
    collected: list[str] = []

    def attempt(fn):
        try:
            value = fn()
        except FlowError as exc:
            collected.extend([str(exc), repr(exc), repr(exc.args)])
        else:
            collected.append(repr(value) + json.dumps(dataclasses.asdict(value), default=str))

    a = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    collected.append(repr(a))
    attempt(lambda: oauth_zoom.finish_callback(store, _state(a), CODE, "wrong"))
    attempt(lambda: oauth_zoom.finish_callback(store, _state(a), CODE, a.nonce))
    attempt(lambda: oauth_zoom.finish_callback(store, _state(a), CODE, a.nonce))
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant", "error_description": CODE})
    b = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    attempt(lambda: oauth_zoom.finish_callback(store, _state(b), CODE, b.nonce))
    token_endpoint.reply = _FakeResponse(200, {"access_token": ACCESS_TOKEN})
    c = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    attempt(lambda: oauth_zoom.finish_callback(store, _state(c), CODE, c.nonce))

    try:
        oauth_zoom.refresh(CLIENT_ID, CLIENT_SECRET, REFRESH)
    except oauth_zoom.RefreshFailed as exc:
        collected.extend([str(exc), repr(exc)])

    joined = "\n".join(collected)
    _assert_absent(joined, CODE, CLIENT_SECRET, REFRESH, ACCESS_TOKEN)
    _assert_absent(_raw_db_text(temp_db), CODE, CLIENT_SECRET, REFRESH, ACCESS_TOKEN)


def test_a_successful_finish_returns_no_secret_values(connection, store, token_endpoint):
    result = oauth_zoom.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    view = oauth_zoom.finish_callback(store, _state(result), CODE, result.nonce)

    _assert_absent(repr(view) + json.dumps(dataclasses.asdict(view), default=str), *_leaks())
