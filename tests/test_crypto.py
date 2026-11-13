from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.services.crypto import SecretCipher


def test_round_trip() -> None:
    cipher = SecretCipher(Fernet.generate_key())
    token = cipher.encrypt("my-torn-api-key")
    assert token != "my-torn-api-key"
    assert cipher.decrypt(token) == "my-torn-api-key"


def test_decrypt_with_wrong_key_raises_value_error() -> None:
    a = SecretCipher(Fernet.generate_key())
    b = SecretCipher(Fernet.generate_key())
    token = a.encrypt("secret")
    with pytest.raises(ValueError, match="decrypt"):
        b.decrypt(token)


def test_from_sources_prefers_env_key() -> None:
    key = Fernet.generate_key().decode()
    cipher = SecretCipher.from_sources(key, key_file=Path("does-not-exist.key"))
    assert cipher.decrypt(cipher.encrypt("x")) == "x"


def test_from_sources_generates_and_persists_key_file(tmp_path: Path) -> None:
    key_file = tmp_path / "secret.key"
    assert not key_file.exists()
    c1 = SecretCipher.from_sources(None, key_file=key_file)
    assert key_file.exists()
    token = c1.encrypt("value")
    # A fresh cipher from the same file decrypts it.
    c2 = SecretCipher.from_sources(None, key_file=key_file)
    assert c2.decrypt(token) == "value"
