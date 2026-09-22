"""
Fernet vault for secrets stored at rest (docs/_research/2026-09-21_management-ui.md section 7).

Pure module on purpose: it imports nothing from pipeline.health or pipeline.connections so both can
depend on it without an import cycle.

The key comes from SIGNALSLATE_SECRET_KEY, a comma-separated list where the first key is primary
(encrypts) and the rest only decrypt, which is what makes rotation possible. Vault.from_env_value
returns None for "no key configured" instead of raising: the caller decides whether that means
"keep running on .env" (the plan's fail-closed choice) or an error.

Nothing here logs or prints a key, a plaintext or a ciphertext, and no exception message carries
one. The `raise ... from None` is deliberate: cryptography's own errors and UnicodeDecodeError can
embed the offending bytes, and chaining would put them back into a traceback.

decrypt is called without a ttl: stored secrets are long lived, an expiring token would silently
break a connection.

The CLI has two commands. `genkey` is the one place a key is ever printed. `rotate` re-encrypts every
stored secret under the primary key (connections.rotate_all) and prints counts and connection ids,
never a key, a secret or a ciphertext; it imports the database layer lazily so the module itself
stays import-cycle free. Rotation is what makes it safe to drop an old key from the list.
"""
import json
import sys
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretKeyMissing(RuntimeError):
    """No key is configured where one is required."""


class SecretKeyInvalid(RuntimeError):
    """A configured key is not a valid Fernet key. The message names its position, never its text."""


class SecretDecryptError(RuntimeError):
    """A token could not be decrypted: wrong key, tampered or malformed. The message is generic."""


def generate_key() -> str:
    """Returns a fresh urlsafe-base64 Fernet key (32 random bytes)."""
    return Fernet.generate_key().decode("ascii")


class Vault:
    """Encrypts under the first key and decrypts under any of them."""

    def __init__(self, keys: list[str]) -> None:
        """
        keys: primary first. Raises SecretKeyMissing for an empty list and SecretKeyInvalid
        (naming the 1-based position only) for a key Fernet rejects.
        """
        if not keys:
            raise SecretKeyMissing("no encryption key provided")
        fernets: list[Fernet] = []
        for position, key in enumerate(keys, start=1):
            try:
                fernets.append(Fernet(key))
            except (ValueError, TypeError):
                raise SecretKeyInvalid(
                    f"encryption key at position {position} is not a valid Fernet key"
                ) from None
        self._multi = MultiFernet(fernets)

    @classmethod
    def from_env_value(cls, value: Optional[str]) -> Optional["Vault"]:
        """
        Parses a comma-separated key list. Blank entries are ignored and None comes back when no
        key is left. The position in a SecretKeyInvalid counts non-blank entries only.
        """
        if value is None:
            return None
        keys = [part.strip() for part in value.split(",") if part.strip()]
        if not keys:
            return None
        return cls(keys)

    def encrypt(self, text: str) -> str:
        """Returns an ASCII Fernet token for `text` under the primary key."""
        return self._multi.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Raises SecretDecryptError (generic message) for any token this vault cannot open."""
        try:
            return self._multi.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            # ValueError covers a non-ASCII token string (base64 rejects it before Fernet sees it)
            # and UnicodeDecodeError on plaintext that is not UTF-8.
            raise SecretDecryptError("could not decrypt the stored secret") from None

    def encrypt_json(self, data: dict[str, Any]) -> str:
        """Envelope for a secrets dict: JSON, then encrypt. Non-ASCII survives the round trip."""
        return self.encrypt(json.dumps(data, ensure_ascii=False))

    def decrypt_json(self, token: str) -> dict[str, Any]:
        """Raises SecretDecryptError if the token does not open or does not hold a JSON object."""
        text = self.decrypt(token)
        try:
            data = json.loads(text)
        except ValueError:
            raise SecretDecryptError("stored secret is not a valid envelope") from None
        if not isinstance(data, dict):
            raise SecretDecryptError("stored secret is not a valid envelope")
        return data

    def rotate(self, token: str) -> str:
        """Re-encrypts `token` under the primary key. Raises SecretDecryptError like decrypt."""
        try:
            return self._multi.rotate(token).decode("ascii")
        except (InvalidToken, ValueError, TypeError):
            raise SecretDecryptError("could not decrypt the stored secret") from None


_GENKEY_REMINDER = (
    "Keep this key in .env as SIGNALSLATE_SECRET_KEY and back it up separately from data/: "
    "losing it loses every stored secret."
)


_USAGE = "usage: python -m pipeline.crypto genkey | rotate"


def _rotate_command() -> int:
    """
    Exit 0 on success, 1 when a row cannot be decrypted or the database fails (nothing changed
    either way), 2 when there is no usable SIGNALSLATE_SECRET_KEY. The key is checked BEFORE the
    database is opened so a misconfigured run never touches data/.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from pipeline import connections, db, health

    try:
        vault = Vault.from_env_value(health.secret_key_setting())
    except SecretKeyInvalid as exc:
        print(f"error: SIGNALSLATE_SECRET_KEY is not usable ({exc})", file=sys.stderr)
        return 2
    if vault is None:
        print("error: SIGNALSLATE_SECRET_KEY is not set; there is no key to rotate to", file=sys.stderr)
        return 2
    try:
        db.init_db()
        result = connections.rotate_all(vault)
    except SQLAlchemyError as exc:
        # Only the class name: SQLAlchemy's message embeds the statement parameters, which here
        # are the ciphertexts.
        print(f"error: database failure ({type(exc).__name__}); nothing was changed", file=sys.stderr)
        return 1
    if result.unreadable:
        print(
            f"error: {len(result.unreadable)} connection(s) cannot be decrypted with the configured "
            "keys; nothing was changed. Keep the old key in SIGNALSLATE_SECRET_KEY, or re-enter the "
            "secrets of: " + ", ".join(result.unreadable),
            file=sys.stderr,
        )
        return 1
    print(
        f"rotated {result.rotated} connection(s) to the primary key; "
        f"{result.without_secrets} had no stored secrets."
    )
    return 0


def main(argv: list[str]) -> int:
    """`genkey` is the one place a key is ever printed; `rotate` prints counts and ids only."""
    if argv == ["genkey"]:
        print(generate_key())
        print(_GENKEY_REMINDER)
        return 0
    if argv == ["rotate"]:
        return _rotate_command()
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # Re-imported under its real name: run as __main__ this file defines its own SecretDecryptError,
    # which connections.rotate_all would not recognise (it catches pipeline.crypto's).
    from pipeline.crypto import main as _main

    sys.exit(_main(sys.argv[1:]))
