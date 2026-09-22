"""
Tests for the namecheap, godaddy and wordpress connection kinds in pipeline.connections
(docs/_research/2026-09-21_domain-collector.md Finding 2).

Mirrors tests/test_connections.py's fixtures and no-secret-leak discipline. Every secret below is a
distinctive invented string so a substring hit can only mean a leak.
"""
import json
import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db  # noqa: E402
from pipeline.connections import InvalidField, MissingField, UnknownField  # noqa: E402

NAMECHEAP_KEY = "namecheap-key-Xy82QpL4vM"
GODADDY_KEY = "godaddy-key-9dWr3ufjTq"
GODADDY_SECRET = "godaddy-secret-2LaEskvNc8"
GODADDY_PAT_TOKEN = "godaddy-pat-6vTq9nWbXe3s"
WORDPRESS_SECRET = "wordpress-secret-8bNcXqE1zR"
WORDPRESS_TOKEN = "wordpress-token-4mPqRstU7d"
ALL_SECRETS = [NAMECHEAP_KEY, GODADDY_KEY, GODADDY_SECRET, GODADDY_PAT_TOKEN, WORDPRESS_SECRET, WORDPRESS_TOKEN]

REGISTRANT_CONTACT = {
    "first_name": "Jane",
    "last_name": "Doe",
    "address1": "123 Example St",
    "city": "Springfield",
    "state_province": "IL",
    "postal_code": "62704",
    "country": "US",
    "phone": "+1.2175551234",
    "email": "jane@example.com",
}

NAMECHEAP = {
    "label": "prod",
    "api_user": "nc_user",
    "username": "nc_user",
    "client_ip": "203.0.113.5",
    "api_key": NAMECHEAP_KEY,
}
# auth_mode "classic": the legacy sso-key key/secret pair. GODADDY_PAT below is the "pat" default
# (Personal Access Token) developer.godaddy.com now issues.
GODADDY = {"label": "prod", "auth_mode": "classic", "api_key": GODADDY_KEY, "api_secret": GODADDY_SECRET}
GODADDY_PAT = {"label": "patacct", "api_token": GODADDY_PAT_TOKEN}
WORDPRESS = {"label": "blog", "client_id": "wp-client-id-123", "client_secret": WORDPRESS_SECRET}


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


def _assert_no_secret(text: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text


# --- namecheap -----------------------------------------------------------------------


def test_create_namecheap_happy_path(vault):
    view = connections.create("namecheap", NAMECHEAP)
    assert (view.id, view.kind, view.label) == ("namecheap_prod", "namecheap", "prod")
    assert view.config == {"label": "prod", "api_user": "nc_user", "username": "nc_user", "client_ip": "203.0.113.5"}
    assert view.secrets_set == ["api_key"]
    assert connections._secrets_for("namecheap_prod") == {"api_key": NAMECHEAP_KEY}


def test_create_namecheap_without_api_user_defaults_to_username(vault):
    """Namecheap's account settings page issues only the API key; ApiUser and UserName are the
    same value (your account username) for every account but a reseller."""
    fields = {k: v for k, v in NAMECHEAP.items() if k != "api_user"}
    view = connections.create("namecheap", fields)
    assert view.config["api_user"] == "nc_user"
    assert view.config["username"] == "nc_user"


def test_create_namecheap_explicit_api_user_is_preserved(vault):
    """The reseller case: ApiUser genuinely differs from UserName."""
    view = connections.create("namecheap", {**NAMECHEAP, "api_user": "nc_reseller"})
    assert view.config["api_user"] == "nc_reseller"
    assert view.config["username"] == "nc_user"


def test_update_namecheap_username_does_not_silently_change_a_different_api_user(vault):
    """A later username edit must not retroactively overwrite an explicit reseller api_user — the
    default only ever applies at create()."""
    connections.create("namecheap", {**NAMECHEAP, "api_user": "nc_reseller"})
    updated = connections.update("namecheap_prod", config={"username": "nc_user_renamed"})
    assert updated.config["username"] == "nc_user_renamed"
    assert updated.config["api_user"] == "nc_reseller"


def test_create_namecheap_sandbox_is_optional_and_validated(vault):
    view = connections.create("namecheap", {**NAMECHEAP, "sandbox": "true"})
    assert view.config["sandbox"] == "true"
    with pytest.raises(InvalidField) as info:
        connections.create("namecheap", {**NAMECHEAP, "label": "other", "sandbox": "yes"})
    assert info.value.field == "sandbox"


def test_create_namecheap_missing_api_key_raises(vault):
    fields = {k: v for k, v in NAMECHEAP.items() if k != "api_key"}
    with pytest.raises(MissingField) as info:
        connections.create("namecheap", fields)
    assert info.value.field == "api_key"


@pytest.mark.parametrize("client_ip", ["2001:db8::1", "999.1.1.1", "192.168.1", "not-an-ip", "192.168.01.1", "", "10.0.0.1/24"])
def test_create_namecheap_rejects_non_ipv4_client_ip(vault, client_ip):
    with pytest.raises(InvalidField) as info:
        connections.create("namecheap", {**NAMECHEAP, "client_ip": client_ip})
    assert info.value.field == "client_ip"
    assert connections.list_connections() == []


@pytest.mark.parametrize("client_ip", ["0.0.0.0", "255.255.255.255", "10.0.0.1"])
def test_create_namecheap_accepts_valid_ipv4(vault, client_ip):
    view = connections.create("namecheap", {**NAMECHEAP, "client_ip": client_ip})
    assert view.config["client_ip"] == client_ip


@pytest.mark.parametrize("label", ["Prod", "PROD", "pr od", "a/b", "../x", "", "x" * 33, "prod\n"])
def test_create_namecheap_label_rejects_bad_characters(vault, label):
    with pytest.raises(InvalidField) as info:
        connections.create("namecheap", {**NAMECHEAP, "label": label})
    assert info.value.field == "label"


def test_create_namecheap_rejects_unknown_field(vault):
    with pytest.raises(UnknownField):
        connections.create("namecheap", {**NAMECHEAP, "token": "not-a-namecheap-field"})


def test_create_namecheap_registrant_contact_accepted_and_normalized(vault):
    view = connections.create("namecheap", {**NAMECHEAP, "registrant_contact": REGISTRANT_CONTACT})
    assert sorted(view.secrets_set) == ["api_key", "registrant_contact"]
    stored = connections.get_secret("namecheap_prod", "registrant_contact")
    assert stored is not None
    assert json.loads(stored) == REGISTRANT_CONTACT


def test_create_namecheap_registrant_contact_accepts_json_string(vault):
    view = connections.create("namecheap", {**NAMECHEAP, "registrant_contact": json.dumps(REGISTRANT_CONTACT)})
    assert "registrant_contact" in view.secrets_set


@pytest.mark.parametrize("missing_key", list(REGISTRANT_CONTACT))
def test_create_namecheap_registrant_contact_requires_every_key(vault, missing_key):
    incomplete = {k: v for k, v in REGISTRANT_CONTACT.items() if k != missing_key}
    with pytest.raises(InvalidField) as info:
        connections.create("namecheap", {**NAMECHEAP, "registrant_contact": incomplete})
    assert info.value.field == "registrant_contact"


@pytest.mark.parametrize("bad", ["not-json", "[1, 2]", '"a string"', 12345, None, {"first_name": "Jane\x00"}])
def test_create_namecheap_registrant_contact_rejects_malformed_input(vault, bad):
    contact = bad if not isinstance(bad, dict) else {**REGISTRANT_CONTACT, **bad}
    with pytest.raises(InvalidField) as info:
        connections.create("namecheap", {**NAMECHEAP, "registrant_contact": contact})
    assert info.value.field == "registrant_contact"


def test_namecheap_no_vault_raises_secret_key_missing():
    with pytest.raises(crypto.SecretKeyMissing):
        connections.create("namecheap", NAMECHEAP)


# --- godaddy ---------------------------------------------------------------------------


def test_create_godaddy_happy_path_defaults_environment(vault):
    view = connections.create("godaddy", GODADDY)
    assert (view.id, view.label) == ("godaddy_prod", "prod")
    assert view.config == {"label": "prod", "auth_mode": "classic", "environment": "production"}
    assert sorted(view.secrets_set) == ["api_key", "api_secret"]
    assert connections._secrets_for("godaddy_prod") == {"api_key": GODADDY_KEY, "api_secret": GODADDY_SECRET}


def test_create_godaddy_accepts_ote_environment(vault):
    view = connections.create("godaddy", {**GODADDY, "environment": "ote"})
    assert view.config["environment"] == "ote"


def test_create_godaddy_rejects_bad_environment(vault):
    with pytest.raises(InvalidField) as info:
        connections.create("godaddy", {**GODADDY, "environment": "staging"})
    assert info.value.field == "environment"


@pytest.mark.parametrize("missing", ["api_key", "api_secret"])
def test_create_godaddy_requires_both_secrets(vault, missing):
    fields = {k: v for k, v in GODADDY.items() if k != missing}
    with pytest.raises(MissingField) as info:
        connections.create("godaddy", fields)
    assert info.value.field == missing


def test_create_godaddy_registrant_contact_is_optional(vault):
    view = connections.create("godaddy", GODADDY)
    assert "registrant_contact" not in view.secrets_set
    updated = connections.update("godaddy_prod", secrets={"registrant_contact": REGISTRANT_CONTACT})
    assert sorted(updated.secrets_set) == ["api_key", "api_secret", "registrant_contact"]


# --- godaddy: pat (Personal Access Token), the auth_mode default -----------------------
# developer.godaddy.com's current signup flow issues a single token, not a key/secret pair; the
# classic pair above comes from the separate, deprecated classic-developer.godaddy.com portal.


def test_create_godaddy_pat_happy_path(vault):
    view = connections.create("godaddy", GODADDY_PAT)
    assert (view.id, view.label) == ("godaddy_patacct", "patacct")
    assert view.secrets_set == ["api_token"]
    assert connections._secrets_for("godaddy_patacct") == {"api_token": GODADDY_PAT_TOKEN}
    assert connections.godaddy_auth_mode(view) == "pat"


def test_create_godaddy_defaults_to_pat_when_auth_mode_omitted(vault):
    """No explicit auth_mode at all (not even "pat") still resolves to pat, api_token required."""
    with pytest.raises(MissingField) as info:
        connections.create("godaddy", {"label": "bare", "api_key": GODADDY_KEY, "api_secret": GODADDY_SECRET})
    assert info.value.field == "api_token"


def test_create_godaddy_pat_mode_requires_api_token(vault):
    with pytest.raises(MissingField) as info:
        connections.create("godaddy", {"label": "nopat", "auth_mode": "pat"})
    assert info.value.field == "api_token"


def test_godaddy_auth_mode_explicit_wins_over_inference(vault):
    """An explicit auth_mode is trusted even if it doesn't match which secret ended up stored (the
    check at create() already enforced consistency; this only proves the reader doesn't re-derive
    once auth_mode is set)."""
    view = connections.create("godaddy", GODADDY)
    assert connections.godaddy_auth_mode(view) == "classic"


# --- wordpress -------------------------------------------------------------------------


def test_create_wordpress_defaults_redirect_mode_and_accepts_no_access_token(vault):
    view = connections.create("wordpress", WORDPRESS)
    assert (view.id, view.label) == ("wordpress_blog", "blog")
    assert view.config == {"label": "blog", "client_id": "wp-client-id-123", "redirect_mode": "paste_back"}
    assert view.secrets_set == ["client_secret"]


def test_create_wordpress_with_callback_mode_and_access_token(vault):
    view = connections.create("wordpress", {**WORDPRESS, "redirect_mode": "callback", "access_token": WORDPRESS_TOKEN})
    assert view.config["redirect_mode"] == "callback"
    assert sorted(view.secrets_set) == ["access_token", "client_secret"]


def test_wordpress_access_token_can_be_set_later_like_a_sign_in_flow(vault):
    connections.create("wordpress", WORDPRESS)
    view = connections.set_secret("wordpress_blog", "access_token", WORDPRESS_TOKEN)
    assert sorted(view.secrets_set) == ["access_token", "client_secret"]


def test_create_wordpress_missing_client_secret_raises(vault):
    fields = {k: v for k, v in WORDPRESS.items() if k != "client_secret"}
    with pytest.raises(MissingField) as info:
        connections.create("wordpress", fields)
    assert info.value.field == "client_secret"


# --- cross-cutting: no leak, no env materialization -------------------------------------


def test_registrar_and_wordpress_connections_never_leak_secrets(vault):
    connections.create("namecheap", {**NAMECHEAP, "registrant_contact": REGISTRANT_CONTACT})
    connections.create("godaddy", GODADDY)
    connections.create("godaddy", GODADDY_PAT)
    connections.create("wordpress", {**WORDPRESS, "access_token": WORDPRESS_TOKEN})
    for view in connections.list_connections():
        _assert_no_secret(json.dumps(view.config))
        _assert_no_secret(repr(view))
        _assert_no_secret(str(view))
    with pytest.raises(MissingField) as info:
        connections.create("namecheap", {"label": "broken"})
    _assert_no_secret(str(info.value))


def test_materialize_emits_nothing_for_namecheap_godaddy_wordpress(vault):
    connections.create("namecheap", NAMECHEAP)
    connections.create("godaddy", GODADDY)
    connections.create("godaddy", GODADDY_PAT)
    connections.create("wordpress", WORDPRESS)
    assert connections.materialize() == {}


@pytest.mark.parametrize(
    "key",
    [
        "NAMECHEAP_PROD_API_KEY",
        "GODADDY_PROD_API_KEY",
        "GODADDY_PROD_API_SECRET",
        "GODADDY_PATACCT_API_TOKEN",
        "WORDPRESS_BLOG_CLIENT_SECRET",
    ],
)
def test_is_family_key_does_not_claim_registrar_style_keys(key):
    assert connections.is_family_key(key) is False
