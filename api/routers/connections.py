"""
Connection management routes (docs/_research/2026-09-21_management-ui.md Decisions 1 and 3).

Design decisions:

- Secrets are write-only. They enter as SecretStr, are unwrapped once to hand to
  pipeline.connections, and no response model has a field that could carry a secret or a
  ciphertext. Every free text that leaves (a stored health detail, a check result) goes through
  redact() with the stored secret values as well as the token shapes.
- A create is inactive until tested: the new id is set OFF in the config toggles before the
  response is returned, so the scheduler cannot collect from credentials nobody has checked.
  Testing does not switch it on; the UI does that through PUT /config.
- config_store.set_source_active silently ignores an id that known_sources() does not contain, and
  known_sources() only sees the connection when the env overlay is registered (it needs a key).
  Without the check the create would succeed and leave a connection that is active by default.
  So membership is verified first and a failed create is rolled back with 503 store_inactive.
- Validation failures from pipeline.connections are turned into the same {type, loc, msg} shape as
  api.security's 422 handler, from the field NAME and generic text the service already carries.
  A raw exception is never rendered.
- The create body avoids pydantic's own discriminated-union and extra-field errors, because both
  echo the offending value (the unknown tag, the unknown key) into `msg` or `loc`, and a
  credential pasted into the wrong box would come back in the response.
- The health checks are blocking network calls, so the handlers are plain `def` and run in the
  threadpool. The test endpoint runs its check inside health.env_snapshot() so it sees one
  consistent overlaid environment.
- namecheap/godaddy/wordpress are registrar connections, not collector sources: they never appear
  in health.known_sources() and have no active_sources entry at all (pipeline/connections.py's
  module docstring; pipeline/config_store.py keys active_sources off known_sources()). The
  "inactive until tested" gate below does not apply to them, so create_connection() returns them
  directly instead of running the known_sources()/set_source_active dance every other kind needs
  (that dance always fails 503 store_inactive for an id known_sources() can never list).
"""
import logging
from typing import Annotated, Any, Literal, Optional, Union

from fastapi import APIRouter, Body, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Discriminator, SecretStr, Tag, model_validator
from sqlmodel import select

from api.serialize import iso_z
from pipeline import config_store, connections, db, health
from pipeline.crypto import SecretDecryptError, SecretKeyMissing
from pipeline.domains import inventory
from pipeline.domains.registrars import AuthFailed, IpNotWhitelisted, NotEligible, RateLimited, RegistrarError
from pipeline.redact import redact

router = APIRouter(tags=["connections"])

_log = logging.getLogger(__name__)

# Same cap the runner applies to text it persists: applied after redaction, never before.
_MAX_TEXT = 1000

# Mirrors pipeline/domains/inventory.py's own _REGISTRAR_KINDS: the connections.KINDS entries with
# a Registrar adapter (inventory.registrar_for) instead of a pipeline.health check_* counterpart.
_REGISTRAR_KINDS = frozenset({"namecheap", "godaddy", "wordpress"})


class _CreateBase(BaseModel):
    """Rejects unknown keys with a message that does not name them (see the module docstring)."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(key not in cls.model_fields for key in data):
            raise ValueError("unknown field")
        return data


class M365Create(_CreateBase):
    kind: Literal["m365"]
    alias: str
    tenant_id: str
    client_id: str


class ZoomCreate(_CreateBase):
    kind: Literal["zoom"]
    client_id: str
    client_secret: SecretStr
    account_id: Optional[str] = None
    auth_mode: Optional[str] = None
    refresh_token: Optional[SecretStr] = None
    redirect_mode: Optional[str] = None
    include_transcripts: Optional[bool] = None


class SlackCreate(_CreateBase):
    kind: Literal["slack"]
    label: str
    token: SecretStr


class GmailCreate(_CreateBase):
    kind: Literal["gmail"]
    label: str
    client_id: str
    client_secret: SecretStr
    refresh_token: Optional[SecretStr] = None
    redirect_mode: Optional[str] = None


class NamecheapCreate(_CreateBase):
    kind: Literal["namecheap"]
    label: str
    username: str
    client_ip: str
    api_key: SecretStr
    # Optional: pipeline.connections.create() defaults it to username (see that KINDS entry's
    # comment) — a fresh Namecheap account only ever issues one credential, the API key.
    api_user: Optional[str] = None
    sandbox: Optional[str] = None
    registrant_contact: Optional[SecretStr] = None


class GodaddyCreate(_CreateBase):
    kind: Literal["godaddy"]
    label: str
    # api_token (pat, the developer.godaddy.com default) or api_key+api_secret (classic, the
    # deprecated sso-key pair) — pipeline.connections._check_godaddy_credentials enforces exactly
    # one shape, keyed by auth_mode; all three are optional here for the same reason ZoomCreate's
    # account_id is.
    api_token: Optional[SecretStr] = None
    api_key: Optional[SecretStr] = None
    api_secret: Optional[SecretStr] = None
    auth_mode: Optional[str] = None
    environment: Optional[str] = None
    registrant_contact: Optional[SecretStr] = None


class WordpressCreate(_CreateBase):
    kind: Literal["wordpress"]
    label: str
    client_id: str
    client_secret: SecretStr
    redirect_mode: Optional[str] = None
    access_token: Optional[SecretStr] = None


def _kind_tag(value: Any) -> Optional[str]:
    kind = value.get("kind") if isinstance(value, dict) else None
    return kind if kind in connections.KINDS else None


# custom_error_* keeps pydantic from putting the rejected tag into the error message.
CreateBody = Annotated[
    Union[
        Annotated[M365Create, Tag("m365")],
        Annotated[ZoomCreate, Tag("zoom")],
        Annotated[SlackCreate, Tag("slack")],
        Annotated[GmailCreate, Tag("gmail")],
        Annotated[NamecheapCreate, Tag("namecheap")],
        Annotated[GodaddyCreate, Tag("godaddy")],
        Annotated[WordpressCreate, Tag("wordpress")],
    ],
    Discriminator(_kind_tag, custom_error_type="invalid_kind", custom_error_message="Unknown or missing kind"),
]


class ConnectionPatch(BaseModel):
    """`config` values are validated (and type-checked) by pipeline.connections, by field name."""

    model_config = ConfigDict(extra="ignore")

    config: Optional[dict[str, Any]] = None
    secrets: Optional[dict[str, SecretStr]] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(key not in cls.model_fields for key in data):
            raise ValueError("unknown field")
        return data


class HealthOut(BaseModel):
    status: str
    detail: Optional[str]
    checked_at: Optional[str]


class ConnectionOut(BaseModel):
    id: str
    kind: str
    label: str
    origin: str
    config: dict[str, str]
    secrets_set: list[str]
    active: bool
    health: Optional[HealthOut]


class CheckResult(BaseModel):
    status: Literal["ok", "error"]
    detail: str


def _scrub(text: Optional[str], known: list[str]) -> Optional[str]:
    """redact() never raises; `known` is passed in so a list reads the store once."""
    if text is None:
        return None
    return redact(text, known, max_len=_MAX_TEXT)


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


def _translate(exc: Exception) -> HTTPException:
    """
    Maps what pipeline.connections and pipeline.crypto raise to an HTTP error. Only the field name
    and the service's generic message are used, never str(exc) of anything unexpected.
    """
    if isinstance(exc, connections.ConnectionNotFound):
        return HTTPException(404, "Connection not found")
    if isinstance(exc, connections.DuplicateConnection):
        return _coded(409, "duplicate_connection", "A connection with this id already exists")
    if isinstance(exc, connections.InvalidConnection):
        return HTTPException(422, [{"type": "value_error", "loc": ["body", exc.field], "msg": exc.message}])
    if isinstance(exc, SecretKeyMissing):
        return _coded(503, "secret_key_missing", "No encryption key is configured; secrets cannot be stored")
    if isinstance(exc, SecretDecryptError):
        return _coded(503, "secret_decrypt_failed", "Stored secrets cannot be decrypted with the installed key")
    raise exc


_HANDLED = (
    connections.ConnectionError,
    SecretKeyMissing,
    SecretDecryptError,
)


def _to_out(
    view: connections.ConnectionView,
    active: dict[str, bool],
    health_rows: dict[str, db.SourceHealth],
    known: list[str],
) -> ConnectionOut:
    row = health_rows.get(view.id)
    return ConnectionOut(
        id=view.id,
        kind=view.kind,
        label=view.label,
        origin=view.origin,
        config={name: str(value) for name, value in view.config.items()},
        secrets_set=view.secrets_set,
        # An id that known_sources() does not list is not collected from, whatever a stale toggle says.
        active=active.get(view.id, False),
        health=None
        if row is None
        else HealthOut(status=row.status, detail=_scrub(row.detail, known), checked_at=iso_z(row.checked_at)),
    )


def _out_for(view: connections.ConnectionView) -> ConnectionOut:
    health_rows = {row.source: row for row in db.latest_source_health()}
    return _to_out(view, config_store.load_config()["active_sources"], health_rows, connections.secret_values())


def _tombstoned_ids() -> set[str]:
    with db.get_session() as session:
        return {row.id for row in session.exec(select(db.Tombstone)).all()}


def _roll_back_create(connection_id: str, had_tombstone: bool) -> None:
    """
    Undoes a create. connections.delete writes a tombstone, which would stop a later .env seeding
    of an id the user never deleted; it is removed again unless one existed before the create
    (create clears it, so this puts it back).
    """
    connections.delete(connection_id)
    if had_tombstone:
        return
    with db.get_session() as session:
        tombstone = session.get(db.Tombstone, connection_id)
        if tombstone is not None:
            session.delete(tombstone)
            session.commit()


def _store_inactive() -> HTTPException:
    return _coded(503, "store_inactive", "The connection store is not active, so this connection cannot be enabled")


@router.get("/connections", response_model=list[ConnectionOut])
def list_all() -> list[ConnectionOut]:
    """Every connection with its toggle and latest health. Never carries a secret."""
    active = config_store.load_config()["active_sources"]
    health_rows = {row.source: row for row in db.latest_source_health()}
    known = connections.secret_values()
    return [_to_out(view, active, health_rows, known) for view in connections.list_connections()]


@router.post("/connections", status_code=201, response_model=ConnectionOut)
def create_connection(body: Annotated[CreateBody, Body()]) -> ConnectionOut:
    """
    Creates a connection, switched off until it has been tested.

    Raises 409 (duplicate), 422 (invalid field, by name), 503 secret_key_missing (secrets but no
    key) and 503 store_inactive (the store's overlay is not registered, create rolled back).
    """
    fields = {
        name: value.get_secret_value() if isinstance(value, SecretStr) else value
        for name, value in body.model_dump(exclude={"kind"}, exclude_none=True).items()
    }
    # pipeline.connections stores every config value as a string; the UI posts include_transcripts
    # as a JSON bool (ZoomCreate.include_transcripts: Optional[bool]).
    if isinstance(fields.get("include_transcripts"), bool):
        fields["include_transcripts"] = "true" if fields["include_transcripts"] else "false"
    had_tombstone_before = _tombstoned_ids()
    try:
        view = connections.create(body.kind, fields)
    except _HANDLED as exc:
        raise _translate(exc) from None

    if body.kind in _REGISTRAR_KINDS:
        return _to_out(view, {}, {}, [])

    had_tombstone = view.id in had_tombstone_before
    if view.id not in health.known_sources():
        _roll_back_create(view.id, had_tombstone)
        raise _store_inactive()
    try:
        merged = config_store.set_source_active(view.id, False)
    except Exception as exc:  # noqa: BLE001 — a failed toggle must not leave the connection on
        _log.error("could not switch %s off after create: %s", view.id, type(exc).__name__)
        _roll_back_create(view.id, had_tombstone)
        raise _store_inactive() from None
    if merged["active_sources"].get(view.id) is not False:
        _roll_back_create(view.id, had_tombstone)
        raise _store_inactive()

    return _to_out(view, merged["active_sources"], {}, [])


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
def update_connection(connection_id: str, body: ConnectionPatch) -> ConnectionOut:
    """
    Replaces the config fields sent and ONLY the secrets sent. An empty-string secret means "leave
    as is": the edit form posts every field, and clearing a stored secret by leaving a box blank
    would be a silent data loss.

    Raises 404, 422 (by field name), 503 secret_key_missing, 503 secret_decrypt_failed.
    """
    secrets = {
        name: value.get_secret_value() for name, value in (body.secrets or {}).items() if value.get_secret_value() != ""
    }
    try:
        view = connections.update(connection_id, config=body.config, secrets=secrets or None)
    except _HANDLED as exc:
        raise _translate(exc) from None
    return _out_for(view)


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(connection_id: str) -> Response:
    """
    Deletes the connection, writes the tombstone, and forgets its toggle and watermark. Collected
    items are kept. Env-origin connections are deletable too: the tombstone stops re-seeding.
    """
    if not connections.delete(connection_id):
        raise HTTPException(404, "Connection not found")
    config_store.forget_source(connection_id)
    db.reset_cursor(connection_id, None)
    return Response(status_code=204)


def _run_registrar_check(view: connections.ConnectionView) -> health.HealthResult:
    """
    Registrar kinds have no pipeline.health check_* counterpart: the credentials live behind
    inventory.registrar_for(), and its check_connection() already raises typed, secret-free
    RegistrarError subclasses (module docstring, pipeline/domains/registrars/__init__.py) that are
    readable as-is — Namecheap's IpNotWhitelisted message already names the egress IP to whitelist.
    """
    try:
        detail = inventory.registrar_for(view.id).check_connection()
    except (NotEligible, IpNotWhitelisted, AuthFailed, RateLimited, RegistrarError) as exc:
        return health.HealthResult(view.id, "error", str(exc))
    return health.HealthResult(view.id, "ok", detail)


def _run_check(view: connections.ConnectionView) -> health.HealthResult:
    if view.kind == "m365":
        return health.check_m365(view.label)
    if view.kind == "zoom":
        return health.check_zoom()
    if view.kind == "slack":
        return health.check_slack(view.label, health.slack_workspaces().get(view.label))
    if view.kind == "gmail":
        return health.check_gmail(view.label)
    return _run_registrar_check(view)


@router.post("/connections/{connection_id}/test", response_model=CheckResult)
def run_connection_test(connection_id: str) -> CheckResult:
    """
    Runs the connection's health check with the stored credentials. It does not switch the
    connection on and does not persist a health row.

    Raises 404 for an unknown id. A check that raises is reported as status error with fixed text:
    the exception's own message may carry a credential.
    """
    view = connections.get(connection_id)
    if view is None:
        raise HTTPException(404, "Connection not found")
    try:
        with health.env_snapshot():
            result = _run_check(view)
    except Exception as exc:  # noqa: BLE001 — every check failure is a result, never a 500
        _log.warning("connection test for %s raised %s", view.id, type(exc).__name__)
        return CheckResult(status="error", detail="The check could not be completed")
    status: Literal["ok", "error"] = "ok" if result.status == "ok" else "error"
    return CheckResult(status=status, detail=_scrub(result.detail, connections.secret_values()) or "")
