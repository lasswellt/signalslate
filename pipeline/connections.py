"""
Validated CRUD for the connection store (docs/_research/2026-09-21_management-ui.md Decisions 1 and 3).

Design decisions:

- The write-only property is the point of this module. ConnectionView carries the NAMES of the
  secrets that have a value (`secrets_set`) and nothing else about them: no value, no ciphertext.
  Secrets are readable only through the module-private _secrets_for, which the materialize step
  uses to build the env overlay.
- Every ConnectionError carries a field NAME and generic text. Nothing that was submitted is echoed
  back, not even for an unknown kind or field name, because a paste into the wrong box would
  otherwise put a credential into an API response and a log line.
- This module must not import pipeline.config_store (its load_config holds a lock while calling
  known_sources(), which reads the store: importing the other way round is a deadlock waiting to
  happen) and must not import pipeline.health at module level (health will import this module's
  overlay provider). Both stay out of the import graph on purpose.
- Ids are derived from the label (m365_<alias>, zoom, slack_<label>, gmail_<label>), so a label can
  never change after creation: update() accepts the label field only when it is unchanged.
- All writes go through one lock. Reads take no lock: SQLite already gives each session a
  consistent snapshot, and the version counter is only a cheap "did anything change" signal for
  the overlay cache.
- The store is authoritative once it exists: seed_from_env copies .env declarations in only for ids
  that have neither a live row nor a tombstone, and never overwrites a row (store wins), so an edit
  made in the UI survives every restart and a deletion is not undone by a stale .env line.
- The rest of the app still reads connections as env-style keys through pipeline.health. materialize
  renders live rows in exactly the key families health parses, and FAMILY_KEY_PATTERNS names them
  so the caller can drop the .env copies of those keys before overlaying the store's.
- Secrets live in one encrypted JSON envelope per connection. A write that carries secrets and has
  no vault raises SecretKeyMissing before anything is touched, because storing plaintext or dropping
  the value silently are both worse than refusing.
- namecheap, godaddy and wordpress are UI-only: they are never seeded from .env and materialize()
  emits nothing for them, because their adapters read credentials through get_secret() directly
  instead of an env-style overlay key. registrant_contact is a secret (it is PII, not a token) held
  as one JSON envelope entry, so it is validated as a JSON object with its required keys and stored
  as a normalized JSON string, the same shape every other secret already has.
- health._live_env drops every family key of the raw env once a key is set, so a .env declaration
  that seed_from_env REJECTED (an alias that fails validation, an over-long label, a Gmail account
  with no client id) would silently stop being collected. seed_from_env therefore keeps those raw
  declarations in memory only (never stored, never logged: unseeded_env_keys() hands out the key
  NAMES) and overlay_provider() adds them back, so they keep running exactly as they did before the
  key was set. The store still wins: a live row or a tombstone for the same id ends the retention.
"""
import ipaddress
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from pipeline import db
from pipeline.clock import utcnow
from pipeline.crypto import SecretDecryptError, SecretKeyMissing, Vault

__all__ = [
    "ConnectionError",
    "InvalidConnection",
    "UnknownKind",
    "UnknownField",
    "MissingField",
    "InvalidField",
    "DuplicateConnection",
    "ConnectionNotFound",
    "ConnectionView",
    "KINDS",
    "set_vault",
    "get_vault",
    "create",
    "update",
    "delete",
    "get",
    "list_connections",
    "exists",
    "set_secret",
    "current_version",
    "get_secret",
    "secret_values",
    "RotationResult",
    "rotate_all",
    "seed_from_env",
    "unseeded_env_keys",
    "materialize",
    "zoom_auth_mode",
    "zoom_include_transcripts",
    "godaddy_auth_mode",
    "overlay_provider",
    "is_family_key",
    "FAMILY_KEY_PATTERNS",
]

_log = logging.getLogger(__name__)


# Deliberately shadows the builtin inside this module: callers import it by name from here. It
# derives from Exception, not the builtin, because this is not an I/O failure.
class ConnectionError(Exception):
    """Base for every error this module raises on purpose. `field` names what was wrong, never its value."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class InvalidConnection(ConnectionError):
    """The submitted data failed validation."""


class UnknownKind(InvalidConnection):
    """The kind is not one of KINDS."""


class UnknownField(InvalidConnection):
    """A field the kind does not have, or one that cannot be changed after creation."""


class MissingField(InvalidConnection):
    """A required field was not supplied."""


class InvalidField(InvalidConnection):
    """A field has the wrong type, length or characters."""


class DuplicateConnection(ConnectionError):
    """A live connection already has this id."""


class ConnectionNotFound(ConnectionError):
    """No connection has this id."""


@dataclass(frozen=True)
class _Kind:
    label_field: Optional[str]  # None for zoom: the label is the literal "zoom"
    config: tuple[str, ...]  # non-secret fields, the label field included
    secrets: tuple[str, ...]
    required_secrets: tuple[str, ...]
    optional_config: tuple[str, ...] = ()


KINDS: dict[str, _Kind] = {
    "m365": _Kind("alias", ("alias", "tenant_id", "client_id"), (), ()),
    "zoom": _Kind(
        None,
        ("client_id", "auth_mode", "account_id", "redirect_mode", "include_transcripts"),
        ("client_secret", "refresh_token"),
        # refresh_token is optional at creation: s2s installs never set it, and oauth installs get it
        # from browser sign-in, same as gmail's refresh_token.
        ("client_secret",),
        optional_config=("auth_mode", "account_id", "redirect_mode", "include_transcripts"),
    ),
    "slack": _Kind("label", ("label",), ("token",), ("token",)),
    "gmail": _Kind(
        "label",
        ("label", "client_id", "redirect_mode"),
        ("client_secret", "refresh_token"),
        # The refresh token is optional at creation: browser sign-in is what stores it.
        ("client_secret",),
        optional_config=("redirect_mode",),
    ),
    "namecheap": _Kind(
        "label",
        ("label", "api_user", "username", "client_ip", "sandbox"),
        ("api_key", "registrant_contact"),
        ("api_key",),
        optional_config=("sandbox",),
    ),
    "godaddy": _Kind(
        "label",
        ("label", "environment", "auth_mode"),
        # api_token: developer.godaddy.com's current signup flow, a single Personal Access Token
        # (Bearer auth). api_key/api_secret: the classic sso-key pair from the older, deprecated
        # classic-developer.godaddy.com portal. required_secrets is empty here on purpose —
        # _check_godaddy_credentials enforces exactly one of the two shapes, keyed by auth_mode.
        ("api_token", "api_key", "api_secret", "registrant_contact"),
        (),
        optional_config=("environment", "auth_mode"),
    ),
    "wordpress": _Kind(
        "label",
        ("label", "client_id", "redirect_mode"),
        # access_token is optional at creation: sign-in is what stores it, same as gmail's refresh_token.
        ("client_secret", "access_token"),
        ("client_secret",),
        optional_config=("redirect_mode",),
    ),
}

_REDIRECT_MODES = ("paste_back", "callback")
_DEFAULT_REDIRECT_MODE = "paste_back"
# Zoom's OAuth app only supports a server-side redirect; the paste_back fallback slack/gmail/wordpress
# offer has no equivalent Zoom flow, so it is not one of the accepted values here.
_ZOOM_REDIRECT_MODES = ("callback",)
_ZOOM_AUTH_MODES = ("oauth", "s2s")
_GODADDY_AUTH_MODES = ("pat", "classic")
_DEFAULT_GODADDY_AUTH_MODE = "pat"
_BOOL_VALUES = ("true", "false")
_ENVIRONMENTS = ("production", "ote")
_DEFAULT_ENVIRONMENT = "production"
# Fields whose value is filled in automatically when the caller omits them.
_CONFIG_DEFAULTS: dict[str, str] = {
    "redirect_mode": _DEFAULT_REDIRECT_MODE,
    "environment": _DEFAULT_ENVIRONMENT,
}
# Kinds whose fields must NOT receive the shared default above: zoom's redirect_mode only accepts
# "callback" (see _ZOOM_REDIRECT_MODES), so the shared "paste_back" default would be invalid for it;
# leaving it unset means "not configured" instead.
_NO_SHARED_DEFAULT: dict[str, set[str]] = {"zoom": {"redirect_mode"}}
_REGISTRANT_CONTACT_KEYS = (
    "first_name",
    "last_name",
    "address1",
    "city",
    "state_province",
    "postal_code",
    "country",
    "phone",
    "email",
)

# The alias becomes a token cache file name, so it must not contain a path character or a dot.
_LABEL_RULES: dict[str, tuple["re.Pattern[str]", int]] = {
    "m365": (re.compile(r"[A-Za-z0-9-]+"), 40),
    "slack": (re.compile(r"[a-z0-9]+"), 32),
    "gmail": (re.compile(r"[a-z0-9]+"), 32),
    "namecheap": (re.compile(r"[a-z0-9]+"), 32),
    "godaddy": (re.compile(r"[a-z0-9]+"), 32),
    "wordpress": (re.compile(r"[a-z0-9]+"), 32),
}
# Tenant ids, client ids and account ids come from provider consoles: GUIDs, dotted Google client
# ids, short tokens and (for a tenant) domain names.
_IDENTIFIER = re.compile(r"[A-Za-z0-9._~@:-]+")
_IDENTIFIER_MAX = 128
_SECRET_MAX = 4096
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

_write_lock = threading.RLock()
_vault: Optional[Vault] = None
_version = 0


@dataclass(frozen=True)
class _Unseeded:
    """
    A .env declaration seed_from_env could not import. `entries` hold raw VALUES and never leave
    this module except through the overlay; `names` are the only part callers may see.

    connection_id: what a stored row for it would be called; an id that cannot be stored (a rejected
    alias) simply never matches a row. m365 entries are keyed by suffix (ALIAS, TENANT_ID,
    CLIENT_ID) because the org number is reassigned when the overlay is built.
    """

    connection_id: str
    names: tuple[str, ...]
    entries: tuple[tuple[str, str], ...]
    m365: bool = False


_unseeded_lock = threading.Lock()
_unseeded: dict[str, _Unseeded] = {}  # keyed by the env key that made the declaration


@dataclass(frozen=True)
class ConnectionView:
    """
    What every caller outside this module gets. `secrets_set` is the sorted list of secret names
    that currently have a value; there is no field that could hold a value or a ciphertext.
    """

    id: str
    kind: str
    label: str
    origin: str
    config: dict[str, Any]
    secrets_set: list[str]
    created_at: datetime
    updated_at: datetime


def set_vault(vault: Optional[Vault]) -> None:
    """Installs (or, with None, removes) the vault. Loaded elsewhere; this module never reads the environment."""
    global _vault
    _vault = vault
    if vault is None:
        _set_unseeded({})


def get_vault() -> Optional[Vault]:
    """Returns the installed vault, or None when no key is configured."""
    return _vault


def current_version() -> int:
    """Counter that every write bumps; the overlay cache compares it to know when to rebuild."""
    return _version


def _bump() -> None:
    global _version
    _version += 1


def _set_unseeded(records: dict[str, _Unseeded]) -> None:
    """Replaces the retained declarations; bumps the version only when they changed so the overlay cache rebuilds."""
    global _unseeded
    with _unseeded_lock:
        changed = _unseeded != records
        _unseeded = records
    if changed:
        _bump()


def _safe_name(name: object) -> str:
    """A submitted key is echoed only when it looks like an identifier: a credential pasted as a key never comes back."""
    if isinstance(name, str) and _SAFE_FIELD_NAME.fullmatch(name):
        return name
    return "<unknown>"


def _require_vault() -> Vault:
    if _vault is None:
        raise SecretKeyMissing("no encryption key is configured")
    return _vault


def _kind_spec(kind: object) -> _Kind:
    if not isinstance(kind, str) or kind not in KINDS:
        raise UnknownKind("kind", "unknown kind")
    return KINDS[kind]


def _validate_config_value(kind: str, name: str, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidField(name, "must be a string")
    if name in ("alias", "label"):
        pattern, max_len = _LABEL_RULES[kind]
        if not value or len(value) > max_len or not pattern.fullmatch(value):
            raise InvalidField(name, f"must match {pattern.pattern} and be at most {max_len} characters")
        return value
    if name == "redirect_mode":
        allowed = _ZOOM_REDIRECT_MODES if kind == "zoom" else _REDIRECT_MODES
        if value not in allowed:
            raise InvalidField(name, f"must be one of {', '.join(allowed)}")
        return value
    if name == "auth_mode":
        allowed = _ZOOM_AUTH_MODES if kind == "zoom" else _GODADDY_AUTH_MODES
        if value not in allowed:
            raise InvalidField(name, f"must be one of {', '.join(allowed)}")
        return value
    if name in ("sandbox", "include_transcripts"):
        if value not in _BOOL_VALUES:
            raise InvalidField(name, f"must be one of {', '.join(_BOOL_VALUES)}")
        return value
    if name == "environment":
        if value not in _ENVIRONMENTS:
            raise InvalidField(name, f"must be one of {', '.join(_ENVIRONMENTS)}")
        return value
    if name == "client_ip":
        try:
            # Namecheap's API allowlist is IPv4 only (Namecheap FAQ); reject IPv6 and anything else.
            ipaddress.IPv4Address(value)
        except ValueError:
            raise InvalidField(name, "must be a valid IPv4 address") from None
        return value
    if not value or len(value) > _IDENTIFIER_MAX or not _IDENTIFIER.fullmatch(value):
        raise InvalidField(name, f"must be 1 to {_IDENTIFIER_MAX} characters of letters, digits and . _ ~ @ : -")
    return value


def _validate_registrant_contact(value: object) -> str:
    """JSON object (or JSON string) with the ICANN registrant fields; stored as a normalized JSON string."""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            raise InvalidField("registrant_contact", "must be a JSON object") from None
    if not isinstance(parsed, dict):
        raise InvalidField("registrant_contact", "must be a JSON object")
    for key in _REGISTRANT_CONTACT_KEYS:
        item = parsed.get(key)
        if not isinstance(item, str) or not item.strip():
            raise InvalidField("registrant_contact", f"must have a non-empty {key}")
    for item in parsed.values():
        if not isinstance(item, str) or _CONTROL_CHARS.search(item):
            raise InvalidField("registrant_contact", "values must be strings with no control characters")
    text = json.dumps(parsed, sort_keys=True)
    if len(text) > _SECRET_MAX:
        raise InvalidField("registrant_contact", f"must be at most {_SECRET_MAX} characters")
    return text


def _validate_secret_value(name: str, value: object) -> str:
    if name == "registrant_contact":
        return _validate_registrant_contact(value)
    if not isinstance(value, str) or not value.strip():
        raise InvalidField(name, "must be a non-empty string")
    if len(value) > _SECRET_MAX or _CONTROL_CHARS.search(value):
        raise InvalidField(name, f"must be at most {_SECRET_MAX} characters with no control characters")
    return value


def _validate_config(kind: str, spec: _Kind, submitted: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in submitted.items():
        if name not in spec.config:
            raise UnknownField(_safe_name(name), "unknown field")
        out[name] = _validate_config_value(kind, name, value)
    return out


def _validate_secrets(spec: _Kind, submitted: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in submitted.items():
        if name not in spec.secrets:
            raise UnknownField(_safe_name(name), "unknown field")
        out[name] = _validate_secret_value(name, value)
    return out


def _check_zoom_auth_mode(config: Mapping[str, str]) -> None:
    """An explicit auth_mode of "s2s" needs account_id on the same effective config (server-to-server
    apps authenticate as an account, so there is nothing to derive it from otherwise). An absent
    auth_mode is never rejected here: zoom_auth_mode() derives it from account_id instead."""
    if config.get("auth_mode") == "s2s" and not config.get("account_id"):
        raise InvalidField("account_id", "required when auth_mode is s2s")


def zoom_auth_mode(view: "ConnectionView") -> str:
    """
    "oauth" or "s2s" for a Zoom connection. An explicit auth_mode wins; otherwise "s2s" when
    account_id is set (the legacy server-to-server shape) and "oauth" otherwise.
    """
    mode = view.config.get("auth_mode")
    if mode in _ZOOM_AUTH_MODES:
        return mode
    return "s2s" if view.config.get("account_id") else "oauth"


def _check_godaddy_credentials(config: Mapping[str, str], secrets: Mapping[str, Any]) -> None:
    """
    developer.godaddy.com's current signup flow issues one Personal Access Token (Bearer auth);
    the classic api_key/api_secret pair (sso-key) comes from a separate, deprecated portal
    (classic-developer.godaddy.com) that most new accounts never see. auth_mode picks which shape
    this connection needs; the default is "pat" since that is what a new signup gets. Checked only
    at create() — like required_secrets, this is a creation-time integrity check, not re-verified
    on every update()."""
    mode = config.get("auth_mode") or _DEFAULT_GODADDY_AUTH_MODE
    if mode == "classic":
        for name in ("api_key", "api_secret"):
            if name not in secrets:
                raise MissingField(name, "required when auth_mode is classic")
    elif "api_token" not in secrets:
        raise MissingField("api_token", "required when auth_mode is pat")


def godaddy_auth_mode(view: "ConnectionView") -> str:
    """
    "pat" or "classic" for a GoDaddy connection. An explicit auth_mode wins; otherwise "classic"
    when api_key is stored (the legacy sso-key shape) and "pat" otherwise, matching what
    developer.godaddy.com issues today.
    """
    mode = view.config.get("auth_mode")
    if mode in _GODADDY_AUTH_MODES:
        return mode
    return "classic" if "api_key" in view.secrets_set else "pat"


def zoom_include_transcripts(view: "ConnectionView") -> bool:
    """
    Whether the Zoom collector should also pull meeting transcripts. Defaults to True (owner decision
    2026-09-21): absent or any value other than the literal "false" means transcripts are included.
    """
    return view.config.get("include_transcripts") != "false"


def _derive(kind: str, spec: _Kind, config: Mapping[str, str]) -> tuple[str, str]:
    """(id, label) from validated config."""
    if spec.label_field is None:
        return kind, kind
    label = config[spec.label_field]
    return f"{kind}_{label}", label


def _envelope(row: db.Connection) -> dict[str, str]:
    """Decrypts the row's secrets. Raises SecretKeyMissing without a vault, SecretDecryptError on a bad token."""
    if not row.secret_ciphertext:
        return {}
    data = _require_vault().decrypt_json(row.secret_ciphertext)
    return {name: value for name, value in data.items() if isinstance(value, str) and value}


def _names_set(row: db.Connection) -> list[str]:
    """
    Names only. A view must stay readable when the key is absent or wrong (the list page has to
    load so the user can see the problem), so a failed decrypt reports no secrets instead of raising.
    """
    try:
        return sorted(_envelope(row))
    except (SecretKeyMissing, SecretDecryptError):
        return []


def _view(row: db.Connection) -> ConnectionView:
    return ConnectionView(
        id=row.id,
        kind=row.kind,
        label=row.label,
        origin=row.origin,
        config=json.loads(row.config),
        secrets_set=_names_set(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def create(kind: str, fields: Mapping[str, Any], origin: str = "ui") -> ConnectionView:
    """
    Creates a connection from a flat dict of config and secret fields, and clears any tombstone
    for its id (an explicit create is the user asking for it back).

    kind: one of KINDS. fields: config and secret fields together. origin: "ui", or "env" when the
    startup seeding creates it.
    Returns the write-only view.
    Raises UnknownKind, UnknownField, MissingField, InvalidField (never carrying a submitted value),
    DuplicateConnection when a live row has the id, SecretKeyMissing when secrets are supplied and
    no vault is installed.
    """
    spec = _kind_spec(kind)
    if not isinstance(fields, Mapping):
        raise InvalidField("fields", "must be an object")
    if origin not in ("ui", "env"):
        raise InvalidField("origin", "must be ui or env")
    config_in = {name: value for name, value in fields.items() if name in spec.config}
    secrets_in = {name: value for name, value in fields.items() if name in spec.secrets}
    for name in fields:
        if name not in config_in and name not in secrets_in:
            raise UnknownField(_safe_name(name), "unknown field")

    config = _validate_config(kind, spec, config_in)
    secrets = _validate_secrets(spec, secrets_in)
    no_default = _NO_SHARED_DEFAULT.get(kind, ())
    for name in spec.optional_config:
        if name in _CONFIG_DEFAULTS and name not in no_default:
            config.setdefault(name, _CONFIG_DEFAULTS[name])
    for name in spec.config:
        if name not in config and name not in spec.optional_config:
            raise MissingField(name, "required")
    for name in spec.required_secrets:
        if name not in secrets:
            raise MissingField(name, "required")
    if kind == "zoom":
        _check_zoom_auth_mode(config)
    elif kind == "godaddy":
        _check_godaddy_credentials(config, secrets)

    connection_id, label = _derive(kind, spec, config)
    ciphertext = _require_vault().encrypt_json(secrets) if secrets else None

    with _write_lock:
        try:
            with db.get_session() as session:
                if session.get(db.Connection, connection_id) is not None:
                    raise DuplicateConnection("id", "a connection with this id already exists")
                highest = session.exec(select(db.Connection.seq).order_by(col(db.Connection.seq).desc())).first()
                seq = (highest or 0) + 1
                now = utcnow()
                row = db.Connection(
                    id=connection_id,
                    kind=kind,
                    label=label,
                    origin=origin,
                    seq=seq,
                    config=json.dumps(config, sort_keys=True),
                    secret_ciphertext=ciphertext,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                tombstone = session.get(db.Tombstone, connection_id)
                if tombstone is not None:
                    session.delete(tombstone)
                session.commit()
                session.refresh(row)
                view = _view(row)
        except IntegrityError:
            raise DuplicateConnection("id", "a connection with this id already exists") from None
        _bump()
    return view


def update(
    connection_id: str,
    config: Optional[Mapping[str, Any]] = None,
    secrets: Optional[Mapping[str, Any]] = None,
) -> ConnectionView:
    """
    Merges `config` into the stored config and replaces ONLY the secret names in `secrets`; every
    other stored secret keeps its value. The label field is accepted only when unchanged, because
    the id is derived from it. Passing neither is a no-op and does not bump the version.

    Returns the write-only view.
    Raises ConnectionNotFound, UnknownField, InvalidField, SecretKeyMissing (secrets supplied with
    no vault), SecretDecryptError (existing secrets cannot be opened with the installed key).
    """
    with _write_lock:
        with db.get_session() as session:
            row = session.get(db.Connection, connection_id)
            if row is None:
                raise ConnectionNotFound("id", "no such connection")
            spec = _kind_spec(row.kind)
            if config is not None and not isinstance(config, Mapping):
                raise InvalidField("config", "must be an object")
            if secrets is not None and not isinstance(secrets, Mapping):
                raise InvalidField("secrets", "must be an object")

            new_config = _validate_config(row.kind, spec, config) if config else {}
            new_secrets = _validate_secrets(spec, secrets) if secrets else {}
            if spec.label_field is not None and spec.label_field in new_config:
                if new_config[spec.label_field] != row.label:
                    raise UnknownField(spec.label_field, "cannot be changed after creation")
            if not new_config and not new_secrets:
                return _view(row)

            if new_secrets:
                merged = _envelope(row)
                merged.update(new_secrets)
                row.secret_ciphertext = _require_vault().encrypt_json(merged)
            if new_config:
                merged_config = json.loads(row.config)
                merged_config.update(new_config)
                if row.kind == "zoom":
                    _check_zoom_auth_mode(merged_config)
                row.config = json.dumps(merged_config, sort_keys=True)
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
            view = _view(row)
        _bump()
    return view


def set_secret(connection_id: str, name: str, value: str) -> ConnectionView:
    """
    Stores one secret, leaving the others alone. Browser sign-in uses it to save the refresh token.

    Raises what update() raises.
    """
    return update(connection_id, secrets={name: value})


def delete(connection_id: str) -> bool:
    """
    Removes the row and writes a Tombstone so the startup .env seeding does not bring it back.

    Returns True when a row was removed, False when there was none (and no tombstone is written:
    an id that never existed has nothing to protect from re-seeding).
    """
    with _write_lock:
        with db.get_session() as session:
            row = session.get(db.Connection, connection_id)
            if row is None:
                return False
            now = utcnow()
            tombstone = session.get(db.Tombstone, connection_id)
            if tombstone is None:
                session.add(db.Tombstone(id=connection_id, deleted_at=now))
            else:
                tombstone.deleted_at = now
                session.add(tombstone)
            session.delete(row)
            session.commit()
        _bump()
    return True


def get(connection_id: str) -> Optional[ConnectionView]:
    """Returns the view, or None when there is no such connection."""
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        return _view(row) if row is not None else None


def list_connections() -> list[ConnectionView]:
    """Every connection in creation order."""
    with db.get_session() as session:
        rows = session.exec(select(db.Connection).order_by(col(db.Connection.seq), col(db.Connection.id))).all()
        return [_view(row) for row in rows]


def exists(connection_id: str) -> bool:
    """True when a live row has this id."""
    with db.get_session() as session:
        return session.get(db.Connection, connection_id) is not None


def _secrets_for(connection_id: str) -> dict[str, str]:
    """
    Decrypted secrets, for the materialize step only: the single door through which a secret value
    leaves this module. Never call it from a route.

    Raises ConnectionNotFound, SecretKeyMissing, SecretDecryptError.
    """
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        if row is None:
            raise ConnectionNotFound("id", "no such connection")
        return _envelope(row)


def get_secret(connection_id: str, name: str) -> Optional[str]:
    """
    One decrypted secret, or None when the connection or the name does not exist.

    For the token-exchange and browser sign-in flows ONLY (they must present the stored client
    secret to the provider). Callers must never log the value, put it in a response, an exception
    message or a query string. Deliberately narrow: it hands out one named value, where
    _secrets_for hands out the whole envelope.

    Raises SecretKeyMissing when no vault is installed and the connection has secrets,
    SecretDecryptError when the stored envelope cannot be opened; both carry generic messages.
    """
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        if row is None:
            return None
        return _envelope(row).get(name)


def secret_values() -> list[str]:
    """
    Every non-empty decrypted secret in the store, to feed redact() so a credential that reaches a
    log line or an error message is scrubbed by value as well as by shape.

    A row that cannot be decrypted (no vault, wrong key) is skipped rather than raised: this runs
    on error paths, and failing there would hide the error being reported.
    """
    found: list[str] = []
    with db.get_session() as session:
        for row in session.exec(select(db.Connection)).all():
            try:
                found.extend(value for value in _envelope(row).values() if value)
            except (SecretKeyMissing, SecretDecryptError):
                continue
    return found


@dataclass(frozen=True)
class RotationResult:
    """
    What rotate_all did, as counts and ids only. A non-empty `unreadable` means nothing was written
    (and `rotated` is then 0). Connection ids come from validated labels, so they are safe to print.
    """

    rotated: int
    without_secrets: int
    unreadable: tuple[str, ...]


def rotate_all(vault: Optional[Vault] = None) -> RotationResult:
    """
    Re-encrypts every row's secret envelope under the primary key, so the old keys can be dropped
    from SIGNALSLATE_SECRET_KEY afterwards. Vault.rotate is only ever called from here: rows
    otherwise stay under whichever key wrote them until each is edited, and removing the old key
    then makes every untouched secret undecryptable.

    Every row is handled whatever its kind: the ciphertext is opaque here, never parsed. It is
    all-or-nothing in ONE transaction: if any row cannot be opened, nothing is written and the
    offending ids are returned, because a half-rotated store would make "which key can I drop?"
    unanswerable. BEGIN IMMEDIATE takes SQLite's write lock before the read: _write_lock only
    serialises writers inside this process, and the CLI runs in another one, so without it an edit
    made through the running API could land between the read and the write and be overwritten with
    a stale envelope. updated_at is left alone (nobody edited the connection) and so is the version counter: the
    decrypted content is unchanged, so a cached overlay stays valid.

    vault: the key list to rotate under; defaults to the installed vault. Old keys must still be in
    it, or those rows are reported as unreadable.
    Returns RotationResult. Raises SecretKeyMissing when there is no vault at all.
    """
    active = vault if vault is not None else _require_vault()
    with _write_lock:
        with db.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            rows = session.exec(select(db.Connection).order_by(col(db.Connection.seq), col(db.Connection.id))).all()
            fresh: dict[str, str] = {}
            unreadable: list[str] = []
            without_secrets = 0
            for row in rows:
                if not row.secret_ciphertext:
                    without_secrets += 1
                    continue
                try:
                    fresh[row.id] = active.rotate(row.secret_ciphertext)
                except SecretDecryptError:
                    unreadable.append(row.id)
            if unreadable:
                session.rollback()
                return RotationResult(0, without_secrets, tuple(unreadable))
            for row in rows:
                if row.id in fresh:
                    row.secret_ciphertext = fresh[row.id]
                    session.add(row)
            session.commit()
    return RotationResult(len(fresh), without_secrets, ())


# Every env-style key a connection owns. is_family_key is what lets the overlay REPLACE these keys
# from the store instead of merging: a stale SLACK_OLD_TOKEN left in .env after the user deleted
# that connection in the UI must not resurrect it.
FAMILY_KEY_PATTERNS: tuple["re.Pattern[str]", ...] = (
    re.compile(r"M365_ORG\d+_.+"),
    re.compile(r"M365_CLIENT_ID"),
    re.compile(r"SLACK_[A-Z0-9]+_TOKEN"),
    re.compile(r"ZOOM_(?:ACCOUNT_ID|CLIENT_ID|CLIENT_SECRET)"),
    re.compile(r"GMAIL_(?:CLIENT_ID|CLIENT_SECRET)"),
    re.compile(r"GMAIL_[A-Z0-9]+_(?:REFRESH_TOKEN|CLIENT_ID|CLIENT_SECRET)"),
)

_M365_ALIAS_KEY = re.compile(r"M365_ORG(\d+)_ALIAS")
_SLACK_TOKEN_KEY = re.compile(r"SLACK_([A-Z0-9]+)_TOKEN")
_GMAIL_TOKEN_KEY = re.compile(r"GMAIL_([A-Z0-9]+)_REFRESH_TOKEN")


def is_family_key(name: str) -> bool:
    """True when `name` is a key a connection owns; SLACK_SKIP_DMS, TZ, ANTHROPIC_API_KEY and the like are not."""
    return any(pattern.fullmatch(name) for pattern in FAMILY_KEY_PATTERNS)


def _declared(raw_env: Mapping[str, Optional[str]], key: str) -> str:
    """The stripped value, "" when unset or blank. A blank line in .env.example is not a declaration."""
    return (raw_env.get(key) or "").strip()


def _has_tombstone(connection_id: str) -> bool:
    with db.get_session() as session:
        return session.get(db.Tombstone, connection_id) is not None


def _seed_one(kind: str, fields: dict[str, str], declared_by: str) -> tuple[Optional[str], bool]:
    """
    Creates one env-origin connection unless the store already decided this id.

    Returns (id when seeded, rejected). rejected is True only when validation refused the
    declaration; an id the store already decided is neither seeded nor rejected.
    """
    spec = KINDS[kind]
    label = fields.get(spec.label_field) if spec.label_field else kind
    connection_id = f"{kind}_{label}" if spec.label_field else kind
    if exists(connection_id) or _has_tombstone(connection_id):
        return None, False
    try:
        return create(kind, fields, origin="env").id, False
    except ConnectionError as exc:
        # Field name and generic text only; the exception never carries a submitted value.
        _log.warning("skipped .env connection declared by %s: %s", declared_by, exc)
        return None, True


def _pairs(raw_env: Mapping[str, Optional[str]], keys: list[str]) -> tuple[tuple[str, str], ...]:
    """(key, raw value) for each declared (non-blank) key, in the order given."""
    found: list[tuple[str, str]] = []
    for key in keys:
        value = raw_env.get(key)
        if value is not None and value.strip():
            found.append((key, value))
    return tuple(found)


def seed_from_env(raw_env: Mapping[str, Optional[str]]) -> list[str]:
    """
    Copies the connections the RAW .env declares into the store, once.

    A connection is created (origin "env") only when its id has no live row and no tombstone, so
    an existing row is never overwritten and a deleted one never comes back. A declaration that
    cannot become a valid connection (an M365 org without a tenant id, a Gmail account without a
    client secret, an alias that fails validation) is skipped with a warning naming the env key,
    never raising, and is kept in memory so overlay_provider() lets it keep running from .env
    (see unseeded_env_keys). Without a vault nothing is seeded, because Slack, Zoom and Gmail
    cannot be stored and a partial import would be surprising.

    raw_env: the unmerged .env mapping. Returns the ids created, in declaration order.
    """
    if _vault is None:
        _set_unseeded({})
        return []
    seeded: list[str] = []
    retained: dict[str, _Unseeded] = {}

    def retain(declared_by: str, record: _Unseeded) -> None:
        # The store decided this id already (a UI row, or a deletion): .env must not resurrect it.
        if exists(record.connection_id) or _has_tombstone(record.connection_id):
            return
        retained[declared_by] = record

    def add(kind: str, fields: dict[str, str], declared_by: str) -> bool:
        created, rejected = _seed_one(kind, fields, declared_by)
        if created is not None:
            seeded.append(created)
        return rejected

    orgs = sorted(
        (int(m.group(1)), key)
        for key in raw_env
        if (m := _M365_ALIAS_KEY.fullmatch(key)) and _declared(raw_env, key)
    )
    shared_m365_client = _declared(raw_env, "M365_CLIENT_ID")
    for number, alias_key in orgs:
        prefix = f"M365_ORG{number}"
        tenant_id = _declared(raw_env, f"{prefix}_TENANT_ID")
        client_id = _declared(raw_env, f"{prefix}_CLIENT_ID") or shared_m365_client

        def retain_org() -> None:
            # The shared client id is resolved into the org's own entry: the shared key is a family
            # key of every org, and the retained org is renumbered when the overlay is built.
            own_client = f"{prefix}_CLIENT_ID"
            client_key = own_client if _declared(raw_env, own_client) else "M365_CLIENT_ID"
            keys = [alias_key, f"{prefix}_TENANT_ID", client_key]
            present = _pairs(raw_env, keys)
            suffixes = {alias_key: "ALIAS", f"{prefix}_TENANT_ID": "TENANT_ID", client_key: "CLIENT_ID"}
            retain(
                alias_key,
                _Unseeded(
                    f"m365_{_declared(raw_env, alias_key)}",
                    tuple(key for key, _ in present),
                    tuple((suffixes[key], value) for key, value in present),
                    m365=True,
                ),
            )

        if not tenant_id or not client_id:
            _log.warning("skipped .env connection declared by %s: tenant id or client id missing", alias_key)
            retain_org()
            continue
        if add("m365", {"alias": _declared(raw_env, alias_key), "tenant_id": tenant_id, "client_id": client_id}, alias_key):
            retain_org()

    zoom_keys = ["ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"]
    zoom = {key[len("ZOOM_"):].lower(): _declared(raw_env, key) for key in zoom_keys}

    def retain_zoom() -> None:
        present = _pairs(raw_env, zoom_keys)
        retain("ZOOM_ACCOUNT_ID", _Unseeded("zoom", tuple(key for key, _ in present), present))

    if any(zoom.values()):
        if all(zoom.values()):
            # A declared ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET trio is always s2s, same as before
            # KINDS grew auth_mode; ZOOM_AUTH_MODE is an optional extra key on top, not a gate.
            zoom_auth_mode = _declared(raw_env, "ZOOM_AUTH_MODE")
            if add("zoom", {**zoom, **({"auth_mode": zoom_auth_mode} if zoom_auth_mode else {})}, "ZOOM_ACCOUNT_ID"):
                retain_zoom()
        else:
            _log.warning("skipped .env connection declared by ZOOM_ACCOUNT_ID: not all three Zoom values are set")
            retain_zoom()

    for key in sorted(raw_env):
        match = _SLACK_TOKEN_KEY.fullmatch(key)
        if match and _declared(raw_env, key):
            if add("slack", {"label": match.group(1).lower(), "token": _declared(raw_env, key)}, key):
                retain(key, _Unseeded(f"slack_{match.group(1).lower()}", (key,), _pairs(raw_env, [key])))

    shared_gmail_id = _declared(raw_env, "GMAIL_CLIENT_ID")
    shared_gmail_secret = _declared(raw_env, "GMAIL_CLIENT_SECRET")
    for key in sorted(raw_env):
        match = _GMAIL_TOKEN_KEY.fullmatch(key)
        if not match or not _declared(raw_env, key):
            continue
        upper = match.group(1)
        client_id = _declared(raw_env, f"GMAIL_{upper}_CLIENT_ID") or shared_gmail_id
        client_secret = _declared(raw_env, f"GMAIL_{upper}_CLIENT_SECRET") or shared_gmail_secret

        def retain_gmail() -> None:
            # Shared client credentials are resolved into per-account keys for the same reason as M365's.
            keys = [key]
            entries = list(_pairs(raw_env, [key]))
            for name, shared in (("CLIENT_ID", "GMAIL_CLIENT_ID"), ("CLIENT_SECRET", "GMAIL_CLIENT_SECRET")):
                own = f"GMAIL_{upper}_{name}"
                source = own if _declared(raw_env, own) else shared
                if _declared(raw_env, source):
                    keys.append(source)
                    entries.append((own, _declared(raw_env, source)))
            retain(key, _Unseeded(f"gmail_{upper.lower()}", tuple(keys), tuple(entries)))

        if not client_id or not client_secret:
            _log.warning("skipped .env connection declared by %s: client id or client secret missing", key)
            retain_gmail()
            continue
        if add(
            "gmail",
            {
                "label": upper.lower(),
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": _declared(raw_env, key),
            },
            key,
        ):
            retain_gmail()

    if retained:
        # Names only: the values stay in _unseeded and reach nothing but the overlay.
        _log.warning(
            "not imported, still running from .env: %s",
            ", ".join(sorted({name for record in retained.values() for name in record.names})),
        )
    _set_unseeded(retained)
    return seeded


def _live_unseeded() -> list[_Unseeded]:
    """
    The retained declarations the store has not since taken over. A live row or a tombstone for the
    id ends the retention for good: the store is authoritative, and a deleted connection must not
    come back through the copy kept here.
    """
    with _unseeded_lock:
        for declared_by, record in list(_unseeded.items()):
            if exists(record.connection_id) or _has_tombstone(record.connection_id):
                del _unseeded[declared_by]
        return list(_unseeded.values())


def unseeded_env_keys() -> list[str]:
    """
    Names (never values) of the .env keys whose declarations seed_from_env rejected and that
    therefore still run from .env. Empty without a vault: nothing was seeded, so nothing is unseeded.
    """
    if _vault is None:
        return []
    return sorted({name for record in _live_unseeded() for name in record.names})


def materialize(*, pending_gmail_token: bool = False) -> dict[str, str]:
    """
    Renders the live connections as the env-style keys pipeline.health already parses.

    pending_gmail_token: emit GMAIL_<L>_REFRESH_TOKEN as "" for a Gmail row that has not signed in
    yet. health anchors gmail_accounts() on that key, so without it a token-less connection is
    invisible to known_sources() and the create route (which checks membership) rejects it, which
    made browser sign-in of a new account unreachable. Only the live overlay asks for it; the default
    keeps the rendering that lists exactly the credentials that exist.

    M365 orgs are numbered 1..N by creation order, so deleting one renumbers the later ones: the
    number is only a key suffix, the alias is what identifies the org everywhere else. A row whose
    secrets cannot be decrypted is left out (with a warning naming the id) instead of raising, so
    one bad row cannot take the other connections down with it.
    """
    out: dict[str, str] = {}
    org_number = 0
    with db.get_session() as session:
        rows = session.exec(
            select(db.Connection).order_by(col(db.Connection.seq), col(db.Connection.created_at), col(db.Connection.id))
        ).all()
        for row in rows:
            config = json.loads(row.config)
            if row.kind == "m365":
                org_number += 1
                prefix = f"M365_ORG{org_number}"
                out[f"{prefix}_ALIAS"] = config["alias"]
                out[f"{prefix}_TENANT_ID"] = config["tenant_id"]
                out[f"{prefix}_CLIENT_ID"] = config["client_id"]
                continue
            try:
                secrets = _envelope(row)
            except (SecretKeyMissing, SecretDecryptError):
                _log.warning("connection %s left out of the overlay: its secrets cannot be decrypted", row.id)
                continue
            upper = row.label.upper()
            if row.kind == "slack":
                if "token" in secrets:
                    out[f"SLACK_{upper}_TOKEN"] = secrets["token"]
            elif row.kind == "gmail":
                # gmail_accounts() maps "" to None, which check_gmail reports as "Not signed in yet".
                if "refresh_token" in secrets:
                    out[f"GMAIL_{upper}_REFRESH_TOKEN"] = secrets["refresh_token"]
                elif pending_gmail_token:
                    out[f"GMAIL_{upper}_REFRESH_TOKEN"] = ""
                out[f"GMAIL_{upper}_CLIENT_ID"] = config["client_id"]
                if "client_secret" in secrets:
                    out[f"GMAIL_{upper}_CLIENT_SECRET"] = secrets["client_secret"]
            elif row.kind == "zoom":
                # oauth mode emits nothing here: the OAuth token flow reads client_id/client_secret
                # straight from the store (like oauth_gmail._client_credentials), and emitting these
                # keys anyway would make the legacy s2s path think an oauth-only connection was s2s.
                mode = config.get("auth_mode")
                if mode not in _ZOOM_AUTH_MODES:
                    mode = "s2s" if config.get("account_id") else "oauth"
                if mode == "s2s":
                    out["ZOOM_ACCOUNT_ID"] = config["account_id"]
                    out["ZOOM_CLIENT_ID"] = config["client_id"]
                    if "client_secret" in secrets:
                        out["ZOOM_CLIENT_SECRET"] = secrets["client_secret"]
    return out


_overlay_lock = threading.Lock()
# (version, vault it was built under, immutable overlay). The vault is part of the key because
# installing a different key changes what decrypts without writing any row.
_overlay_cache: Optional[tuple[int, Optional[Vault], Mapping[str, str]]] = None


def overlay_provider() -> Optional[Mapping[str, str]]:
    """
    The materialized overlay, or None when no vault is installed so the caller keeps pure .env
    behaviour. Cached and rebuilt only when a write bumped current_version() or the vault changed.

    The version is read BEFORE building: a write that lands mid-build leaves the cached version
    behind the counter, so the next call rebuilds instead of serving a stale overlay. The result is
    a read-only mapping, safe to share between threads.
    """
    global _overlay_cache
    vault = _vault
    if vault is None:
        return None
    with _overlay_lock:
        version = _version
        cached = _overlay_cache
        if cached is not None and cached[0] == version and cached[1] is vault:
            return cached[2]
        merged = materialize(pending_gmail_token=True)
        # Retained .env declarations are numbered after the stored orgs: materialize numbers the store
        # 1..N, and reusing an env number would overwrite a stored org's keys.
        org_number = sum(1 for key in merged if _M365_ALIAS_KEY.fullmatch(key))
        for record in _live_unseeded():
            if record.m365:
                org_number += 1
                merged.update({f"M365_ORG{org_number}_{suffix}": value for suffix, value in record.entries})
            else:
                for key, value in record.entries:
                    merged.setdefault(key, value)
        overlay: Mapping[str, str] = MappingProxyType(merged)
        _overlay_cache = (version, vault, overlay)
        return overlay
