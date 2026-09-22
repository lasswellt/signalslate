"""
Unit tests for pipeline.crypto. Everything runs against the real cryptography library; nothing is
mocked. Secret discipline: every exception message, repr and formatted traceback is checked for
the plaintext, the ciphertext and the key text.
"""
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import crypto  # noqa: E402
from pipeline.crypto import (  # noqa: E402
    SecretDecryptError,
    SecretKeyInvalid,
    SecretKeyMissing,
    Vault,
    generate_key,
)

PLAINTEXT = "hunter2-not-a-real-secret"


def _everything(exc: BaseException) -> str:
    """Every text an exception could leak through: str, repr, args and the formatted traceback."""
    return " ".join(
        [str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))]
    )


def test_round_trip():
    vault = Vault([generate_key()])
    token = vault.encrypt(PLAINTEXT)
    assert PLAINTEXT not in token
    assert vault.decrypt(token) == PLAINTEXT


def test_encrypt_is_not_deterministic():
    vault = Vault([generate_key()])
    assert vault.encrypt(PLAINTEXT) != vault.encrypt(PLAINTEXT)


def test_generate_key_is_a_valid_fernet_key():
    key = generate_key()
    Fernet(key)
    assert generate_key() != key


def test_wrong_key_raises_decrypt_error():
    token = Vault([generate_key()]).encrypt(PLAINTEXT)
    other = Vault([generate_key()])
    with pytest.raises(SecretDecryptError) as info:
        other.decrypt(token)
    text = _everything(info.value)
    assert PLAINTEXT not in text
    assert token not in text


@pytest.mark.parametrize("bad", ["", "not-a-token", "gAAAAA" + "x" * 40, "é" * 20])
def test_malformed_token_raises_decrypt_error(bad):
    vault = Vault([generate_key()])
    with pytest.raises(SecretDecryptError) as info:
        vault.decrypt(bad)
    assert bad not in _everything(info.value) or bad == ""


def test_tampered_token_is_rejected():
    vault = Vault([generate_key()])
    token = vault.encrypt(PLAINTEXT)
    flipped = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretDecryptError):
        vault.decrypt(flipped)


def test_decrypt_error_has_no_chained_cause():
    vault = Vault([generate_key()])
    with pytest.raises(SecretDecryptError) as info:
        vault.decrypt("garbage-token")
    assert info.value.__suppress_context__ is True
    assert info.value.__cause__ is None


def test_rotation_old_token_opens_with_new_primary():
    old, new = generate_key(), generate_key()
    token = Vault([old]).encrypt(PLAINTEXT)

    rotating = Vault([new, old])
    assert rotating.decrypt(token) == PLAINTEXT

    rotated = rotating.rotate(token)
    assert rotated != token
    assert Vault([new]).decrypt(rotated) == PLAINTEXT
    with pytest.raises(SecretDecryptError):
        Vault([old]).decrypt(rotated)


def test_new_tokens_use_the_primary_key():
    old, new = generate_key(), generate_key()
    token = Vault([new, old]).encrypt(PLAINTEXT)
    assert Vault([new]).decrypt(token) == PLAINTEXT
    with pytest.raises(SecretDecryptError):
        Vault([old]).decrypt(token)


def test_rotate_rejects_a_token_no_key_opens():
    token = Vault([generate_key()]).encrypt(PLAINTEXT)
    with pytest.raises(SecretDecryptError) as info:
        Vault([generate_key()]).rotate(token)
    text = _everything(info.value)
    assert PLAINTEXT not in text
    assert token not in text


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n", ",", " , ,, ", ",,,"])
def test_blank_env_values_mean_no_vault(value):
    assert Vault.from_env_value(value) is None


def test_from_env_value_splits_strips_and_keeps_order():
    old, new = generate_key(), generate_key()
    vault = Vault.from_env_value(f" {new} , ,{old},")
    assert vault is not None
    token = Vault([old]).encrypt(PLAINTEXT)
    assert vault.decrypt(token) == PLAINTEXT
    assert Vault([new]).decrypt(vault.encrypt(PLAINTEXT)) == PLAINTEXT


def test_empty_key_list_raises_missing():
    with pytest.raises(SecretKeyMissing):
        Vault([])


def test_malformed_key_names_position_not_text():
    bad = "this-is-not-a-fernet-key-SENTINEL"
    good = generate_key()
    with pytest.raises(SecretKeyInvalid) as info:
        Vault([good, bad])
    text = _everything(info.value)
    assert "position 2" in str(info.value)
    assert bad not in text
    assert "SENTINEL" not in text
    assert good not in text


def test_malformed_key_from_env_value_skips_blanks_when_counting():
    bad = "malformed-SENTINEL"
    with pytest.raises(SecretKeyInvalid) as info:
        Vault.from_env_value(f" , {generate_key()} ,, {bad}")
    assert "position 2" in str(info.value)
    assert "SENTINEL" not in _everything(info.value)


@pytest.mark.parametrize("bad", ["short", "é" * 44, "!" * 44])
def test_assorted_malformed_keys_are_rejected(bad):
    with pytest.raises(SecretKeyInvalid) as info:
        Vault([bad])
    assert bad not in _everything(info.value)


def test_encrypt_json_unicode_round_trip():
    vault = Vault([generate_key()])
    data = {"client_secret": "pässwörd-日本語-🔑", "nested": {"n": 3}, "empty": ""}
    token = vault.encrypt_json(data)
    assert vault.decrypt_json(token) == data
    assert "pässwörd" not in token


def test_encrypt_unicode_text_round_trip():
    vault = Vault([generate_key()])
    assert vault.decrypt(vault.encrypt("日本語🔑")) == "日本語🔑"


def test_decrypt_json_rejects_non_object_and_non_json():
    vault = Vault([generate_key()])
    for text in ("just a string", '["a", "b"]', "42"):
        token = vault.encrypt(text)
        with pytest.raises(SecretDecryptError) as info:
            vault.decrypt_json(token)
        assert text not in _everything(info.value)
        assert token not in _everything(info.value)


def test_decrypt_json_wrong_key_raises_decrypt_error():
    token = Vault([generate_key()]).encrypt_json({"k": PLAINTEXT})
    with pytest.raises(SecretDecryptError) as info:
        Vault([generate_key()]).decrypt_json(token)
    assert PLAINTEXT not in _everything(info.value)


def test_non_utf8_plaintext_raises_decrypt_error_without_bytes():
    key = generate_key()
    token = Fernet(key).encrypt(b"\xff\xfe-SENTINEL").decode("ascii")
    with pytest.raises(SecretDecryptError) as info:
        Vault([key]).decrypt(token)
    assert "SENTINEL" not in _everything(info.value)


def test_genkey_cli_prints_a_valid_key_then_a_reminder():
    result = subprocess.run(
        [sys.executable, "-m", "pipeline.crypto", "genkey"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    Fernet(lines[0])
    for phrase in (".env", "separately from data/", "loses every stored secret"):
        assert phrase in lines[1]
    assert lines[0] not in lines[1]
    assert result.stderr == ""


def test_cli_rejects_unknown_commands_without_printing_a_key(capsys):
    assert crypto.main(["bogus"]) == 2
    assert crypto.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage" in captured.err


def test_module_never_prints_on_import_or_use(capsys):
    vault = Vault([generate_key()])
    vault.decrypt(vault.encrypt(PLAINTEXT))
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
