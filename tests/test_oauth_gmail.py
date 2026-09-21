"""
Tests for pipeline.oauth_gmail. The token endpoint is stubbed at requests.post inside
pipeline.oauth_google (the one true external); the flow store, the connection store, the vault and the
SQLite file are all real.

Above all: the code, the verifier, the refresh token and the client secret must not appear in any
exception message, repr or returned value. Every secret below is a distinctive invented string so a
substring hit can only mean a leak.
"""
import base64
import dataclasses
import hashlib
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlmodel import SQLModel, create_engine

from pipeline import connections, crypto, db, oauth_flows, oauth_gmail, oauth_google
from pipeline.health import GMAIL_SCOPE
from pipeline.oauth_flows import (
    ExpiredFlow,
    FlowError,
    FlowStore,
    InvalidCallbackConfig,
    InvalidPastedUrl,
    NonceMismatch,
    UnknownFlow,
)

CLIENT_ID = "1234-example.apps.googleusercontent.com"
CLIENT_SECRET = "gmail-client-secret-Tj40cUe5Ya"
REFRESH = "gmail-refresh-Vn27sGf1Lo"
CODE = "4/auth-code-Zk29qLmXw"
BASE_URL = "https://signalslate.example.com"
CONNECTION_ID = "gmail_personal"


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
    """A Gmail connection with a client but no refresh token, as it is right before browser sign-in."""
    return connections.create("gmail", {"label": "personal", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})


@pytest.fixture
def store():
    return FlowStore()


@pytest.fixture
def token_endpoint(monkeypatch):
    """Stub the token endpoint; `.calls` holds the form bodies it received, `.reply` what it answers."""

    class Endpoint:
        calls: list[dict] = []
        reply = _FakeResponse(200, {"refresh_token": REFRESH, "scope": GMAIL_SCOPE, "access_token": "at-Qm18"})

    endpoint = Endpoint()
    endpoint.calls = []

    def fake_post(url, data, timeout=None, **kwargs):
        assert url == oauth_google.TOKEN_ENDPOINT
        endpoint.calls.append(dict(data))
        return endpoint.reply

    monkeypatch.setattr(oauth_google.requests, "post", fake_post)
    return endpoint


def _state(start_result) -> str:
    return parse_qs(urlsplit(start_result.auth_url).query)["state"][0]


def _paste_url(start_result, code=CODE, port=oauth_gmail.PASTE_BACK_PORT, extra="") -> str:
    return f"http://127.0.0.1:{port}/?state={_state(start_result)}&code={code}&scope=x{extra}"


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
    return (CODE, CLIENT_SECRET, REFRESH, *extra)


def _fail(exc_type, fn, *forbidden: str) -> FlowError:
    """Run fn, expect exc_type, and prove nothing sensitive is in its message, repr or traceback chain."""
    with pytest.raises(exc_type) as info:
        fn()
    exc = info.value
    _assert_absent(str(exc) + repr(exc) + repr(exc.args), *_leaks(*forbidden))
    assert exc.__cause__ is None
    return exc


# ---- start ----


def test_start_paste_back_builds_the_consent_url(connection, store):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    query = parse_qs(urlsplit(result.auth_url).query)
    assert result.auth_url.startswith(oauth_google.AUTH_ENDPOINT + "?")
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"http://127.0.0.1:{oauth_gmail.PASTE_BACK_PORT}"]
    assert query["scope"] == [GMAIL_SCOPE]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert result.mode == "paste_back"
    assert result.nonce and result.flow_id and result.instructions
    assert len(store) == 1
    assert CLIENT_SECRET not in result.auth_url


def test_start_challenge_is_the_s256_of_the_stored_verifier(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    verifier = token_endpoint.calls[0]["code_verifier"]
    challenge = parse_qs(urlsplit(result.auth_url).query)["code_challenge"][0]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_start_callback_uses_the_public_base_url(connection, store):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL + "/")

    query = parse_qs(urlsplit(result.auth_url).query)
    assert query["redirect_uri"] == [f"{BASE_URL}/api/oauth/callback/google"]
    assert result.mode == "callback"


def test_start_result_repr_hides_the_nonce_and_the_state(connection, store):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    text = repr(result)
    assert result.nonce not in text
    assert _state(result) not in text


def test_start_callback_without_public_base_url_is_refused(connection, store):
    _fail(oauth_gmail.CallbackUnavailable, lambda: oauth_gmail.start(CONNECTION_ID, "callback", store=store))
    assert len(store) == 0


def test_callback_unavailable_is_an_invalid_callback_config():
    assert issubclass(oauth_gmail.CallbackUnavailable, InvalidCallbackConfig)


@pytest.mark.parametrize("base", ["http://signalslate.example.com", "not a url", ""])
def test_start_callback_needs_an_https_base_url(connection, store, base):
    with pytest.raises(InvalidCallbackConfig):
        oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=base)
    assert len(store) == 0


def test_start_rejects_an_unknown_mode(connection, store):
    with pytest.raises(oauth_gmail.InvalidMode):
        oauth_gmail.start(CONNECTION_ID, "device", store=store)


def test_start_unknown_or_non_gmail_connection(vault, store):
    connections.create("slack", {"label": "work", "token": "slack-token-Hd91XvB7mR"})

    with pytest.raises(oauth_gmail.UnknownConnection):
        oauth_gmail.start("gmail_nope", "paste_back", store=store)
    with pytest.raises(oauth_gmail.UnknownConnection):
        oauth_gmail.start("slack_work", "paste_back", store=store)


def _edit_row(connection_id, *, drop_client_id=False, drop_secrets=False):
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        assert row is not None
        if drop_client_id:
            config = json.loads(row.config)
            config.pop("client_id")
            row.config = json.dumps(config)
        if drop_secrets:
            row.secret_ciphertext = None
        session.add(row)
        session.commit()


def test_start_needs_the_client_secret(connection, store):
    _edit_row(CONNECTION_ID, drop_secrets=True)

    _fail(oauth_gmail.ClientCredentialsMissing, lambda: oauth_gmail.start(CONNECTION_ID, "paste_back", store=store))
    assert len(store) == 0


def test_start_needs_the_client_id(connection, store):
    _edit_row(CONNECTION_ID, drop_client_id=True)

    _fail(oauth_gmail.ClientCredentialsMissing, lambda: oauth_gmail.start(CONNECTION_ID, "paste_back", store=store))


# ---- finish: happy paths ----


def test_paste_back_happy_path_stores_the_refresh_token_encrypted(connection, store, token_endpoint, temp_db):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    view = oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    assert view.id == CONNECTION_ID
    assert "refresh_token" in view.secrets_set and "client_secret" in view.secrets_set
    assert _decrypted()["refresh_token"] == REFRESH
    assert _decrypted()["client_secret"] == CLIENT_SECRET
    raw = _raw_db_text(temp_db)
    _assert_absent(raw, REFRESH, CLIENT_SECRET)
    _assert_absent(json.dumps(dataclasses.asdict(view), default=str) + repr(view), *_leaks())
    assert len(store) == 0


def test_exchange_sends_the_code_the_stored_verifier_and_the_stored_redirect(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    (body,) = token_endpoint.calls
    assert body["code"] == CODE
    assert body["client_id"] == CLIENT_ID
    assert body["client_secret"] == CLIENT_SECRET
    assert body["grant_type"] == "authorization_code"
    assert body["redirect_uri"] == f"http://127.0.0.1:{oauth_gmail.PASTE_BACK_PORT}"
    assert len(body["code_verifier"]) >= 43


def test_the_exchange_uses_the_stored_redirect_uri_not_the_pasted_one(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    pasted = _paste_url(result, port=9999).replace("127.0.0.1", "localhost")

    oauth_gmail.finish_paste(store, result.flow_id, pasted, result.nonce)

    (body,) = token_endpoint.calls
    assert body["redirect_uri"] == f"http://127.0.0.1:{oauth_gmail.PASTE_BACK_PORT}"
    assert "9999" not in body["redirect_uri"] and "localhost" not in body["redirect_uri"]


def test_callback_happy_path_stores_the_refresh_token_encrypted(connection, store, token_endpoint, temp_db):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    view = oauth_gmail.finish_callback(store, _state(result), CODE, result.nonce)

    assert "refresh_token" in view.secrets_set
    assert _decrypted()["refresh_token"] == REFRESH
    _assert_absent(_raw_db_text(temp_db), REFRESH, CLIENT_SECRET)
    (body,) = token_endpoint.calls
    assert body["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/google"
    assert body["code"] == CODE


def test_a_second_sign_in_replaces_the_refresh_token(connection, store, token_endpoint):
    first = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    oauth_gmail.finish_paste(store, first.flow_id, _paste_url(first), first.nonce)
    token_endpoint.reply = _FakeResponse(200, {"refresh_token": "gmail-refresh-Ab55", "scope": GMAIL_SCOPE})
    second = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    oauth_gmail.finish_paste(store, second.flow_id, _paste_url(second), second.nonce)

    assert _decrypted()["refresh_token"] == "gmail-refresh-Ab55"
    assert _decrypted()["client_secret"] == CLIENT_SECRET


# ---- finish: refusals ----


def test_scope_missing_is_refused_and_nothing_is_stored(connection, store, token_endpoint):
    token_endpoint.reply = _FakeResponse(200, {"refresh_token": REFRESH, "scope": "openid email"})
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        oauth_gmail.ScopeNotGranted,
        lambda: oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )

    assert "refresh_token" not in _decrypted()


def test_no_refresh_token_is_refused_and_nothing_is_stored(connection, store, token_endpoint):
    token_endpoint.reply = _FakeResponse(200, {"access_token": "at-Qm18", "scope": GMAIL_SCOPE})
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(oauth_gmail.NoRefreshToken, lambda: oauth_gmail.finish_callback(store, _state(result), CODE, result.nonce))

    assert "refresh_token" not in _decrypted()


def test_exchange_rejected_reports_no_provider_text(connection, store, token_endpoint):
    token_endpoint.reply = _FakeResponse(
        400, {"error": "invalid_grant", "error_description": f"Bad code {CODE} for {CLIENT_SECRET}"}
    )
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    exc = _fail(
        oauth_gmail.ExchangeFailed,
        lambda: oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )

    assert "invalid_grant" not in str(exc)
    assert "refresh_token" not in _decrypted()


def test_network_failure_on_the_exchange_is_reported_generically(connection, store, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError(f"failed posting {CODE} with {CLIENT_SECRET}")

    monkeypatch.setattr(oauth_google.requests, "post", boom)
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        oauth_gmail.ExchangeFailed,
        lambda: oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )


def test_denied_consent_via_paste(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    denied = f"http://127.0.0.1:8765/?state={_state(result)}&error=access_denied&error_description=user+said+no"

    exc = _fail(oauth_gmail.ConsentDenied, lambda: oauth_gmail.finish_paste(store, result.flow_id, denied, result.nonce))

    assert "user said no" not in str(exc)
    assert token_endpoint.calls == []
    assert len(store) == 0


def test_denied_consent_via_callback(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_gmail.ConsentDenied,
        lambda: oauth_gmail.finish_callback(store, _state(result), None, result.nonce, error="access_denied"),
    )

    assert token_endpoint.calls == []
    assert len(store) == 0


def test_other_provider_errors_are_not_reported_as_a_denial(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_gmail.ProviderError,
        lambda: oauth_gmail.finish_callback(store, _state(result), None, result.nonce, error="server_error"),
    )


def test_callback_without_a_code_is_an_error_and_makes_no_request(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    with pytest.raises(oauth_gmail.ProviderError):
        oauth_gmail.finish_callback(store, _state(result), None, result.nonce)

    assert token_endpoint.calls == []


# ---- finish: state, nonce, replay, expiry ----


def test_replaying_the_same_state_fails(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    oauth_gmail.finish_callback(store, _state(result), CODE, result.nonce)

    _fail(UnknownFlow, lambda: oauth_gmail.finish_callback(store, _state(result), CODE, result.nonce))

    assert len(token_endpoint.calls) == 1


def test_replaying_the_pasted_url_fails(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    pasted = _paste_url(result)
    oauth_gmail.finish_paste(store, result.flow_id, pasted, result.nonce)

    _fail(UnknownFlow, lambda: oauth_gmail.finish_paste(store, result.flow_id, pasted, result.nonce))

    assert len(token_endpoint.calls) == 1


def test_expired_flow(connection, store, token_endpoint, monkeypatch):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    later = oauth_flows.utcnow() + timedelta(seconds=oauth_flows.DEFAULT_TTL_SECONDS + 1)
    monkeypatch.setattr(oauth_flows, "utcnow", lambda: later)

    _fail(ExpiredFlow, lambda: oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce))

    assert token_endpoint.calls == []
    assert len(store) == 0


def test_a_wrong_nonce_is_refused_and_does_not_burn_the_flow(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(NonceMismatch, lambda: oauth_gmail.finish_callback(store, _state(result), CODE, "someone-elses-nonce"))
    _fail(NonceMismatch, lambda: oauth_gmail.finish_callback(store, _state(result), CODE, None))
    assert token_endpoint.calls == []

    oauth_gmail.finish_callback(store, _state(result), CODE, result.nonce)
    assert len(token_endpoint.calls) == 1


def test_a_forged_callback_state_is_unknown(connection, store, token_endpoint):
    oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(UnknownFlow, lambda: oauth_gmail.finish_callback(store, "forged-state", CODE, "any"))
    _fail(UnknownFlow, lambda: oauth_gmail.finish_callback(store, None, CODE, "any"))
    assert token_endpoint.calls == []


def test_a_pasted_url_for_another_sign_in_is_a_state_mismatch(connection, store, token_endpoint):
    first = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    second = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        oauth_gmail.StateMismatch,
        lambda: oauth_gmail.finish_paste(store, first.flow_id, _paste_url(second), first.nonce),
    )

    assert token_endpoint.calls == []


def test_a_malformed_paste_does_not_burn_the_flow(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        InvalidPastedUrl,
        lambda: oauth_gmail.finish_paste(
            store, result.flow_id, f"https://evil.example.org/?state={_state(result)}&code={CODE}", result.nonce
        ),
    )
    assert len(store) == 1
    assert token_endpoint.calls == []

    oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)
    assert len(token_endpoint.calls) == 1


def test_a_callback_flow_cannot_be_consumed_as_another_provider(connection, store):
    result = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    flow = store.consume(state=_state(result), nonce=result.nonce, provider="google")

    assert flow.provider == "google"
    assert "code_verifier" not in repr(flow)


# ---- finish: connection removed mid-flow ----


def test_connection_deleted_mid_flow(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    connections.delete(CONNECTION_ID)

    _fail(
        oauth_gmail.UnknownConnection,
        lambda: oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )

    assert token_endpoint.calls == []


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

    a = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)
    collected.append(repr(a))
    attempt(lambda: oauth_gmail.finish_paste(store, a.flow_id, _paste_url(a), "wrong"))
    attempt(lambda: oauth_gmail.finish_paste(store, a.flow_id, _paste_url(a, code=CODE), a.nonce))
    attempt(lambda: oauth_gmail.finish_paste(store, a.flow_id, _paste_url(a, code=CODE), a.nonce))
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant", "error_description": CODE})
    b = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    attempt(lambda: oauth_gmail.finish_callback(store, _state(b), CODE, b.nonce))
    token_endpoint.reply = _FakeResponse(200, {"refresh_token": REFRESH, "scope": "openid"})
    c = oauth_gmail.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    attempt(lambda: oauth_gmail.finish_callback(store, _state(c), CODE, c.nonce))

    verifier = token_endpoint.calls[0]["code_verifier"]
    joined = "\n".join(collected)
    _assert_absent(joined, CODE, CLIENT_SECRET, REFRESH, verifier)
    _assert_absent(_raw_db_text(temp_db), CODE, CLIENT_SECRET, REFRESH, verifier)


def test_a_successful_finish_returns_no_secret_values(connection, store, token_endpoint):
    result = oauth_gmail.start(CONNECTION_ID, "paste_back", store=store)

    view = oauth_gmail.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    verifier = token_endpoint.calls[0]["code_verifier"]
    _assert_absent(repr(view) + json.dumps(dataclasses.asdict(view), default=str), *_leaks(verifier))
