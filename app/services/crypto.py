"""Symmetric encryption for secrets stored in the database.

Per-guild Torn / FFScouter API keys are supplied by faction admins via
``/setup`` and must not sit in the database in clear text. They are encrypted
with Fernet (AES-128-CBC + HMAC, from the ``cryptography`` package) using a
single key held by the bot operator.

Key resolution order:

1. ``CONFIG_ENCRYPTION_KEY`` environment variable (a urlsafe-base64 32-byte
   Fernet key), or
2. a ``config/secret.key`` file (git-ignored). If neither exists one is
   generated and written there, and a warning is logged — **back that file up**,
   losing it means every server has to re-run ``/setup``.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_KEY_FILE = Path("config/secret.key")


class SecretCipher:
    """Wraps a Fernet key. Construct once and share."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_sources(
        cls, env_key: str | None, *, key_file: Path = DEFAULT_KEY_FILE
    ) -> SecretCipher:
        if env_key:
            return cls(env_key.encode("utf-8"))
        if key_file.exists():
            return cls(key_file.read_bytes().strip())
        key = Fernet.generate_key()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        logger.warning(
            "crypto.key_generated",
            path=str(key_file),
            note="new encryption key generated - back this file up; "
            "losing it means every server must re-run /setup",
        )
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        token: bytes = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            plaintext: bytes = self._fernet.decrypt(token.encode("ascii"))
            return plaintext.decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "could not decrypt a stored secret - the encryption key has "
                "changed or the value is corrupt"
            ) from exc
