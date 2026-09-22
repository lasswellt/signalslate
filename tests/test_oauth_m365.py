"""
Tests for pipeline.oauth_m365. MSAL is the true external here: PublicClientApplication is replaced with a
fake exposing initiate_auth_code_flow, acquire_token_by_auth_code_flow and get_accounts, and NO network
call is made. The flow store, the connection store, the vault, tokencache and the SQLite file are real,
and the token directory is a temp dir (the repository's own tokens/ directory must stay untouched).

Above all: the authorization code, the PKCE verifier, the access and refresh tokens and MSAL's error text
must not appear in any exception message, repr or returned value. Every secret below is a distinctive
invented string so a substring hit can only mean a leak.
"""
import base64
import dataclasses
import json
import logging
import stat
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlmodel import Session, SQLModel, create_engine

from pipeline import connections, crypto, db, health, oauth_flows, oauth_gmail, oauth_m365, tokencache
from pipeline.oauth_flows import (
    ExpiredFlow,
    FlowError,
    FlowStore,
    InvalidCallbackConfig,
    InvalidPastedUrl,
    NonceMismatch,
    ProviderMismatch,
    UnknownFlow,
)

TENANT_ID = "00000000-1111-2222-3333-444444444444"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ALIAS = "acme"
CONNECTION_ID = "m365_acme"
ACCOUNT = "alex@example.com"
BASE_URL = "https://signalslate.example.com"

CODE = "0.AXsA-auth-code-Zk29qLmXw"
VERIFIER = "pkce-verifier-Rt61xHd0Pa"
ACCESS = "access-token-Qm18wEs7Uc"
REFRESH = "refresh-token-Vn27sGf1Lo"
ERROR_TEXT = "AADSTS70008: The provided authorization code or refresh token has expired. " + "trace-detail " * 400

REPO_TOKENS = tokencache.ROOT / "tokens"


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
def token_dir(monkeypatch, tmp_path):
    """health.TOKEN_DIR is what the health check and collectors read; the module must write there."""
    directory = tmp_path / "tokens"
    monkeypatch.setattr(health, "TOKEN_DIR", directory)
    return directory


@pytest.fixture
def connection(vault, token_dir):
    return connections.create("m365", {"alias": ALIAS, "tenant_id": TENANT_ID, "client_id": CLIENT_ID})


@pytest.fixture
def store():
    return FlowStore()


class FakeMsal:
    """Stands in for msal.PublicClientApplication; `.reply` is what acquire_token_by_auth_code_flow returns."""

    def __init__(self):
        self.constructed: list[dict] = []
        self.initiate_calls: list[dict] = []
        self.acquire_calls: list[dict] = []
        self.initiate_error: Exception | None = None
        self.acquire_error: Exception | None = None
        self.flow_reply: dict | None = None
        self.reply: dict = {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "id_token_claims": {"preferred_username": ACCOUNT},
        }
        self.accounts: list[dict] = [{"username": "fallback@example.com"}]
        self.delay = 0.0
        self.max_active = 0
        self._active = 0
        self._guard = threading.Lock()

    def app_class(self):
        fake = self

        class FakeApp:
            def __init__(self, client_id, authority=None, token_cache=None, **kwargs):
                assert token_cache is not None
                self.cache = token_cache
                fake.constructed.append({"client_id": client_id, "authority": authority, "cache": token_cache})

            def initiate_auth_code_flow(self, scopes, redirect_uri=None, **kwargs):
                fake.initiate_calls.append({"scopes": scopes, "redirect_uri": redirect_uri, **kwargs})
                if fake.initiate_error:
                    raise fake.initiate_error
                if fake.flow_reply is not None:
                    return fake.flow_reply
                state = f"state-{len(fake.initiate_calls)}-{time.monotonic_ns()}"
                return {
                    "state": state,
                    "redirect_uri": redirect_uri,
                    "scope": list(scopes),
                    "code_verifier": VERIFIER,
                    "auth_uri": (
                        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
                        f"?client_id={CLIENT_ID}&state={state}&code_challenge=c&response_type=code"
                    ),
                }

            def acquire_token_by_auth_code_flow(self, auth_code_flow, auth_response, **kwargs):
                fake.acquire_calls.append({"flow": auth_code_flow, "response": dict(auth_response), "kwargs": kwargs})
                with fake._guard:
                    fake._active += 1
                    fake.max_active = max(fake.max_active, fake._active)
                try:
                    if fake.delay:
                        time.sleep(fake.delay)
                    if fake.acquire_error:
                        raise fake.acquire_error
                    if auth_response.get("state") != auth_code_flow["state"]:
                        raise ValueError(f"State mismatch for {auth_response.get('code')}")
                    if "access_token" in fake.reply:
                        self.cache.add(
                            {
                                "client_id": CLIENT_ID,
                                "scope": ["Mail.Read"],
                                "token_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                                "response": {
                                    "access_token": fake.reply["access_token"],
                                    "refresh_token": fake.reply.get("refresh_token", REFRESH),
                                    "expires_in": 3600,
                                    "token_type": "Bearer",
                                    "id_token_claims": fake.reply.get("id_token_claims", {}),
                                    "client_info": base64.urlsafe_b64encode(
                                        json.dumps({"uid": "u1", "utid": TENANT_ID}).encode()
                                    ).decode(),
                                },
                                "data": {},
                            }
                        )
                    return dict(fake.reply)
                finally:
                    with fake._guard:
                        fake._active -= 1

            def get_accounts(self):
                return fake.accounts

        return FakeApp


@pytest.fixture
def fake_msal(monkeypatch):
    fake = FakeMsal()
    monkeypatch.setattr(oauth_m365.msal, "PublicClientApplication", fake.app_class())
    return fake


def _state(result) -> str:
    return parse_qs(urlsplit(result.auth_url).query)["state"][0]


def _paste_url(result, code=CODE, extra="") -> str:
    return f"http://localhost/?code={code}&state={_state(result)}&session_state=abc{extra}"


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
    return (CODE, VERIFIER, ACCESS, REFRESH, "trace-detail", *extra)


def _fail(exc_type, fn, *forbidden: str) -> FlowError:
    """Run fn, expect exc_type, and prove nothing sensitive is in its message, repr or cause chain."""
    with pytest.raises(exc_type) as info:
        fn()
    exc = info.value
    _assert_absent(str(exc) + repr(exc) + repr(exc.args), *_leaks(*forbidden))
    assert exc.__cause__ is None
    return exc


def _cache_file(name=ALIAS):
    return health.TOKEN_DIR / f"{name}_cache.bin"


@pytest.fixture(autouse=True)
def repo_tokens_untouched():
    before = REPO_TOKENS.exists()
    yield
    assert REPO_TOKENS.exists() == before, "a test touched the repository tokens/ directory"


# ---- start ----


def test_start_paste_back_asks_msal_for_a_query_mode_flow(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    assert fake_msal.constructed[0]["client_id"] == CLIENT_ID
    assert fake_msal.constructed[0]["authority"] == f"https://login.microsoftonline.com/{TENANT_ID}"
    call = fake_msal.initiate_calls[0]
    assert call["scopes"] == health.SCOPES
    assert call["redirect_uri"] == "http://localhost"
    # Only the default response_mode (query) puts the code in the address bar the user copies from.
    assert "response_mode" not in call
    assert result.auth_url.startswith(f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize?")
    assert result.mode == "paste_back"
    assert result.nonce and result.flow_id
    assert "minute" in result.instructions
    assert result.expires_at > oauth_flows.utcnow()
    assert len(store) == 1


def test_start_returns_the_gmail_start_result_shape(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    assert type(result) is oauth_gmail.StartResult
    assert [f.name for f in dataclasses.fields(result)] == [
        "flow_id", "auth_url", "mode", "expires_at", "nonce", "instructions"
    ]


def test_start_result_repr_hides_the_nonce_and_the_state(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    text = repr(result)
    assert result.nonce not in text
    assert _state(result) not in text


def test_start_keeps_the_whole_flow_dict_server_side(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    _assert_absent(repr(result) + result.auth_url + result.instructions, VERIFIER)
    flow = store.consume(flow_id=result.flow_id, nonce=result.nonce, provider="microsoft")
    assert flow.payload["code_verifier"] == VERIFIER
    assert flow.redirect_uri == "http://localhost"
    assert flow.state == _state(result)


def test_start_callback_uses_the_public_base_url(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL + "/")

    assert fake_msal.initiate_calls[0]["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/microsoft"
    assert result.mode == "callback"
    assert "returned here" in result.instructions


def test_start_callback_without_public_base_url_is_refused(connection, fake_msal, store):
    _fail(oauth_m365.CallbackUnavailable, lambda: oauth_m365.start(CONNECTION_ID, "callback", store=store))
    assert len(store) == 0
    assert fake_msal.initiate_calls == []


@pytest.mark.parametrize("base", ["http://signalslate.example.com", "not a url", ""])
def test_start_callback_needs_an_https_base_url(connection, fake_msal, store, base):
    with pytest.raises(InvalidCallbackConfig):
        oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=base)
    assert len(store) == 0


def test_start_rejects_an_unknown_mode(connection, fake_msal, store):
    _fail(oauth_m365.InvalidMode, lambda: oauth_m365.start(CONNECTION_ID, "popup", store=store))
    assert len(store) == 0


def test_start_unknown_connection(connection, fake_msal, store):
    _fail(oauth_m365.UnknownConnection, lambda: oauth_m365.start("m365_missing", "paste_back", store=store))
    assert len(store) == 0


def test_start_refuses_a_connection_of_another_kind(connection, fake_msal, store):
    connections.create(
        "gmail", {"label": "personal", "client_id": "x.apps.googleusercontent.com", "client_secret": "cs-Ab12"}
    )

    _fail(oauth_m365.UnknownConnection, lambda: oauth_m365.start("gmail_personal", "paste_back", store=store))
    assert len(store) == 0


def test_start_msal_failure_is_a_safe_provider_error(connection, fake_msal, store):
    fake_msal.initiate_error = ValueError(f"Unable to get authority configuration for {CODE}")

    _fail(oauth_m365.ProviderError, lambda: oauth_m365.start(CONNECTION_ID, "paste_back", store=store))
    assert len(store) == 0


def test_start_network_failure_is_a_safe_provider_error(connection, fake_msal, store):
    fake_msal.initiate_error = requests.ConnectionError(f"connection refused {CODE}")

    _fail(oauth_m365.ProviderError, lambda: oauth_m365.start(CONNECTION_ID, "paste_back", store=store))


@pytest.mark.parametrize("reply", [{}, {"state": "s", "auth_uri": None}, {"auth_uri": "https://x.example/a"}])
def test_start_refuses_a_flow_without_state_or_auth_uri(connection, fake_msal, store, reply):
    fake_msal.flow_reply = reply

    _fail(oauth_m365.ProviderError, lambda: oauth_m365.start(CONNECTION_ID, "paste_back", store=store))
    assert len(store) == 0


def _insert_raw_connection(engine, alias):
    """connections.create refuses a hostile alias, so plant the row the way a corrupted database would hold it."""
    with Session(engine) as session:
        session.add(
            db.Connection(
                id=f"m365_{alias}".replace("/", "_"),
                kind="m365",
                label=alias,
                origin="ui",
                config=json.dumps({"alias": alias, "tenant_id": TENANT_ID, "client_id": CLIENT_ID}),
            )
        )
        session.commit()
    return f"m365_{alias}".replace("/", "_")


@pytest.mark.parametrize("alias", ["../evil", "a/b", "acme\n", "", "a.b"])
def test_a_traversal_alias_is_rejected_before_anything_is_started(vault, temp_db, token_dir, fake_msal, store, alias):
    connection_id = _insert_raw_connection(temp_db, alias)

    _fail(oauth_m365.InvalidAlias, lambda: oauth_m365.start(connection_id, "paste_back", store=store))
    assert len(store) == 0
    assert fake_msal.constructed == []
    assert not token_dir.exists()


def test_a_traversal_alias_is_rejected_at_finish(vault, temp_db, token_dir, fake_msal, store, tmp_path):
    connection_id = _insert_raw_connection(temp_db, "../evil")
    flow, nonce = store.create("microsoft", connection_id, "paste_back", "st", "http://localhost", {"state": "st"})

    _fail(
        oauth_m365.InvalidAlias,
        lambda: oauth_m365.finish_callback(store, "st", CODE, nonce),
    )
    assert fake_msal.acquire_calls == []
    assert not token_dir.exists()
    assert not (tmp_path / "evil_cache.bin").exists()


# ---- finish: happy paths ----


def test_finish_paste_writes_the_cache_for_the_alias(connection, fake_msal, store, token_dir):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    outcome = oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    path = _cache_file()
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert REFRESH in path.read_text()
    assert sorted(p.name for p in token_dir.iterdir()) == [f"{ALIAS}_cache.bin"]
    assert outcome.connection.id == CONNECTION_ID
    assert outcome.account == ACCOUNT
    assert len(store) == 0


def test_finish_paste_hands_msal_the_stored_flow_and_only_code_and_state(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    oauth_m365.finish_paste(store, result.flow_id, _paste_url(result, extra="&client_info=zzz"), result.nonce)

    call = fake_msal.acquire_calls[0]
    assert call["flow"]["code_verifier"] == VERIFIER
    assert call["flow"]["redirect_uri"] == "http://localhost"
    assert call["response"] == {"code": CODE, "state": _state(result)}


def test_finish_callback_writes_the_cache_for_the_alias(vault, token_dir, fake_msal, store):
    connections.create("m365", {"alias": "contoso-eu", "tenant_id": TENANT_ID, "client_id": CLIENT_ID})
    result = oauth_m365.start("m365_contoso-eu", "callback", store=store, public_base_url=BASE_URL)

    outcome = oauth_m365.finish_callback(store, _state(result), CODE, result.nonce)

    assert sorted(p.name for p in token_dir.iterdir()) == ["contoso-eu_cache.bin"]
    assert stat.S_IMODE(_cache_file("contoso-eu").stat().st_mode) == 0o600
    assert outcome.connection.id == "m365_contoso-eu"
    assert fake_msal.acquire_calls[0]["response"] == {"code": CODE, "state": _state(result)}
    assert fake_msal.acquire_calls[0]["flow"]["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/microsoft"


def test_finish_keeps_an_existing_cache_entry(connection, fake_msal, store, token_dir):
    token_dir.mkdir()
    previous = {"Account": {"old-account-key": {"username": "old@example.com"}}}
    _cache_file().write_text(json.dumps(previous))
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    saved = json.loads(_cache_file().read_text())
    assert "old-account-key" in saved["Account"]
    assert REFRESH in json.dumps(saved)
    assert fake_msal.constructed[-1]["cache"] is not None


def test_account_label_falls_back_to_the_msal_account(connection, fake_msal, store):
    fake_msal.reply = {"access_token": ACCESS, "refresh_token": REFRESH}
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    outcome = oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    assert outcome.account == "fallback@example.com"


def test_nothing_secret_is_returned_or_persisted_in_the_database(connection, fake_msal, store, temp_db):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    outcome = oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    returned = repr(outcome) + repr(dataclasses.asdict(outcome)) + repr(result)
    _assert_absent(returned, CODE, VERIFIER, ACCESS, REFRESH, _state(result), result.nonce)
    _assert_absent(_raw_db_text(temp_db), CODE, VERIFIER, ACCESS, REFRESH)
    assert outcome.connection.secrets_set == []


# ---- finish: failures ----


def test_msal_state_mismatch_is_a_generic_error(connection, fake_msal, store, token_dir):
    result = oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)
    # MSAL's own check is the second line of defence: the flow's state is what the fake compares against.
    fake_msal.acquire_error = ValueError(f"State mismatch for {CODE} with {VERIFIER}")

    _fail(oauth_m365.StateMismatch, lambda: oauth_m365.finish_callback(store, _state(result), CODE, result.nonce))
    assert not token_dir.exists()
    assert len(store) == 0


def test_a_pasted_state_from_another_sign_in_is_refused_before_msal(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)
    url = f"http://localhost/?code={CODE}&state=someone-elses-state"

    _fail(oauth_m365.StateMismatch, lambda: oauth_m365.finish_paste(store, result.flow_id, url, result.nonce))
    assert fake_msal.acquire_calls == []
    _fail(UnknownFlow, lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce))


def test_an_error_response_is_bounded_and_carries_no_token(connection, fake_msal, store, token_dir, caplog):
    fake_msal.reply = {"error": "invalid_grant", "error_description": ERROR_TEXT, "access_token": ACCESS}
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    with caplog.at_level(logging.DEBUG):
        exc = _fail(
            oauth_m365.ExchangeFailed,
            lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
            "AADSTS70008",
            "authorization code or refresh token",
        )

    assert len(str(exc)) < 200
    assert not token_dir.exists()
    logged = caplog.text
    assert "AADSTS70008" in logged
    _assert_absent(logged, CODE, VERIFIER, ACCESS, "trace-detail")


def test_a_response_without_an_access_token_is_a_failure(connection, fake_msal, store, token_dir):
    fake_msal.reply = {"token_type": "Bearer"}
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        oauth_m365.ExchangeFailed,
        lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )
    assert not token_dir.exists()


def test_a_network_failure_is_an_exchange_failure_without_a_cause(connection, fake_msal, store):
    fake_msal.acquire_error = requests.ConnectionError(f"POST https://login.example.org/token?code={CODE}")
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        oauth_m365.ExchangeFailed,
        lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )


def test_a_cache_write_failure_is_reported_without_leaking(connection, fake_msal, store, token_dir):
    token_dir.write_text("a file where the directory should be")
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    _fail(
        oauth_m365.CacheWriteFailed,
        lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
        str(token_dir),
    )


def test_a_connection_deleted_mid_flow_is_refused(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)
    connections.delete(CONNECTION_ID)

    _fail(
        oauth_m365.UnknownConnection,
        lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce),
    )
    assert fake_msal.acquire_calls == []


def test_a_paste_without_a_code_is_a_provider_error(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(oauth_m365.ProviderError, lambda: oauth_m365.finish_callback(store, _state(result), None, result.nonce))
    assert fake_msal.acquire_calls == []


def test_consent_denied_in_a_pasted_url(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)
    url = f"http://localhost/?error=access_denied&error_description=user+said+no&state={_state(result)}"

    _fail(oauth_m365.ConsentDenied, lambda: oauth_m365.finish_paste(store, result.flow_id, url, result.nonce))
    assert fake_msal.acquire_calls == []
    _fail(UnknownFlow, lambda: oauth_m365.finish_paste(store, result.flow_id, url, result.nonce))


def test_another_error_in_a_pasted_url_is_a_provider_error(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)
    url = f"http://localhost/?error=server_error&state={_state(result)}"

    _fail(oauth_m365.ProviderError, lambda: oauth_m365.finish_paste(store, result.flow_id, url, result.nonce))


def test_a_provider_error_callback_without_a_code_is_consumed_as_consent_denied(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_m365.ConsentDenied,
        lambda: oauth_m365.finish_callback(store, _state(result), None, result.nonce, error="access_denied"),
    )
    assert len(store) == 0
    assert fake_msal.acquire_calls == []
    _fail(UnknownFlow, lambda: oauth_m365.finish_callback(store, _state(result), CODE, result.nonce))


def test_another_provider_error_callback_is_a_provider_error(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL)

    _fail(
        oauth_m365.ProviderError,
        lambda: oauth_m365.finish_callback(store, _state(result), None, result.nonce, error="server_error"),
    )
    assert len(store) == 0


# ---- the store's rules still apply ----


def test_a_malformed_paste_does_not_burn_the_flow(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    for bad in ("http://evil.example/?code=x&state=y", "not a url", f"http://localhost/#code={CODE}&state=y"):
        with pytest.raises(InvalidPastedUrl):
            oauth_m365.finish_paste(store, result.flow_id, bad, result.nonce)
    assert len(store) == 1

    oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)
    assert len(store) == 0


@pytest.mark.parametrize("nonce", [None, "", "wrong-nonce"])
def test_a_wrong_or_missing_nonce_leaves_the_flow_and_never_reaches_msal(connection, fake_msal, store, nonce):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)

    with pytest.raises(NonceMismatch):
        oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), nonce)
    with pytest.raises(NonceMismatch):
        oauth_m365.finish_callback(store, _state(result), CODE, nonce)
    assert len(store) == 1
    assert fake_msal.acquire_calls == []


def test_a_flow_can_only_finish_once(connection, fake_msal, store):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)
    oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)

    with pytest.raises(UnknownFlow):
        oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce)
    assert len(fake_msal.acquire_calls) == 1


def test_an_expired_flow_is_refused_before_msal(connection, fake_msal, store, monkeypatch, token_dir):
    result = oauth_m365.start(CONNECTION_ID, "paste_back", store=store)
    later = oauth_flows.utcnow() + timedelta(seconds=oauth_flows.DEFAULT_TTL_SECONDS + 1)
    monkeypatch.setattr(oauth_flows, "utcnow", lambda: later)

    _fail(ExpiredFlow, lambda: oauth_m365.finish_paste(store, result.flow_id, _paste_url(result), result.nonce))
    assert fake_msal.acquire_calls == []
    assert not token_dir.exists()


def test_a_google_flow_cannot_be_finished_as_microsoft(connection, fake_msal, store):
    _, nonce = store.create("google", "gmail_personal", "callback", "g-state", "https://x.example/cb", {})

    with pytest.raises(ProviderMismatch):
        oauth_m365.finish_callback(store, "g-state", CODE, nonce)
    assert len(store) == 1


# ---- concurrency ----


def test_concurrent_finishes_on_one_alias_are_serialized(connection, fake_msal, store):
    fake_msal.delay = 0.05
    starts = [oauth_m365.start(CONNECTION_ID, "callback", store=store, public_base_url=BASE_URL) for _ in range(4)]
    barrier = threading.Barrier(len(starts))
    errors: list[BaseException] = []

    def run(start_result):
        barrier.wait()
        try:
            oauth_m365.finish_callback(store, _state(start_result), CODE, start_result.nonce)
        except BaseException as exc:  # surfaced below; a thread must not swallow it
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in starts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(fake_msal.acquire_calls) == len(starts)
    assert fake_msal.max_active == 1
    assert json.loads(_cache_file().read_text())


# ---- errors ----


@pytest.mark.parametrize(
    "exc_type",
    [
        oauth_m365.InvalidMode,
        oauth_m365.UnknownConnection,
        oauth_m365.InvalidAlias,
        oauth_m365.CallbackUnavailable,
        oauth_m365.StateMismatch,
        oauth_m365.ConsentDenied,
        oauth_m365.ProviderError,
        oauth_m365.ExchangeFailed,
        oauth_m365.CacheWriteFailed,
    ],
)
def test_every_error_is_a_flow_error_with_a_fixed_message_and_no_arguments(exc_type):
    assert issubclass(exc_type, FlowError)
    exc = exc_type()
    assert exc.args == (exc_type.message,)
    with pytest.raises(TypeError):
        exc_type("a code")  # type: ignore[call-arg]


def test_provider_neutral_errors_are_shared_with_gmail_and_the_rest_extend_them():
    assert oauth_m365.InvalidMode is oauth_gmail.InvalidMode
    assert oauth_m365.CallbackUnavailable is oauth_gmail.CallbackUnavailable
    assert oauth_m365.StateMismatch is oauth_gmail.StateMismatch
    assert issubclass(oauth_m365.ConsentDenied, oauth_gmail.ConsentDenied)
    assert issubclass(oauth_m365.ProviderError, oauth_gmail.ProviderError)
    assert issubclass(oauth_m365.ExchangeFailed, oauth_gmail.ExchangeFailed)
    assert issubclass(oauth_m365.UnknownConnection, oauth_gmail.UnknownConnection)
    for exc_type in (oauth_m365.ConsentDenied, oauth_m365.ProviderError, oauth_m365.ExchangeFailed):
        assert "Google" not in exc_type.message
