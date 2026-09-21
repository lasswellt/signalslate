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
- Secrets live in one encrypted JSON envelope per connection. A write that carries secrets and has
  no vault raises SecretKeyMissing before anything is touched, because storing plaintext or dropping
  the value silently are both worse than refusing.
"""
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
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
]


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
    "zoom": _Kind(None, ("account_id", "client_id"), ("client_secret",), ("client_secret",)),
    "slack": _Kind("label", ("label",), ("token",), ("token",)),
    "gmail": _Kind(
        "label",
        ("label", "client_id", "redirect_mode"),
        ("client_secret", "refresh_token"),
        # The refresh token is optional at creation: browser sign-in is what stores it.
        ("client_secret",),
        optional_config=("redirect_mode",),
    ),
}

_REDIRECT_MODES = ("paste_back", "callback")
_DEFAULT_REDIRECT_MODE = "paste_back"

# The alias becomes a token cache file name, so it must not contain a path character or a dot.
_LABEL_RULES: dict[str, tuple["re.Pattern[str]", int]] = {
    "m365": (re.compile(r"[A-Za-z0-9-]+"), 40),
    "slack": (re.compile(r"[a-z0-9]+"), 32),
    "gmail": (re.compile(r"[a-z0-9]+"), 32),
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


def get_vault() -> Optional[Vault]:
    """Returns the installed vault, or None when no key is configured."""
    return _vault


def current_version() -> int:
    """Counter that every write bumps; the overlay cache compares it to know when to rebuild."""
    return _version


def _bump() -> None:
    global _version
    _version += 1


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
        if value not in _REDIRECT_MODES:
            raise InvalidField(name, f"must be one of {', '.join(_REDIRECT_MODES)}")
        return value
    if not value or len(value) > _IDENTIFIER_MAX or not _IDENTIFIER.fullmatch(value):
        raise InvalidField(name, f"must be 1 to {_IDENTIFIER_MAX} characters of letters, digits and . _ ~ @ : -")
    return value


def _validate_secret_value(name: str, value: object) -> str:
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
    if "redirect_mode" in spec.optional_config:
        config.setdefault("redirect_mode", _DEFAULT_REDIRECT_MODE)
    for name in spec.config:
        if name not in config:
            raise MissingField(name, "required")
    for name in spec.required_secrets:
        if name not in secrets:
            raise MissingField(name, "required")

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
