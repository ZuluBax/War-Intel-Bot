"""Shared test fixtures."""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.config.settings import Settings


class FakeTornClient:
    """Stand-in for TornClient: matches request paths to canned responses.

    ``routes`` maps a regex (matched against the path passed to ``get``) to
    either a dict (returned as-is) or a callable ``(path) -> dict``. Unmatched
    paths raise, so a test notices an unexpected call.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = [(re.compile(pattern), value) for pattern, value in routes.items()]
        self.calls: list[str] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(path)
        for pattern, value in self._routes:
            if pattern.search(path):
                return value(path) if callable(value) else value
        raise AssertionError(f"FakeTornClient: no route for {path!r}")

    async def aclose(self) -> None:  # pragma: no cover - parity with the real client
        pass


@pytest.fixture
def settings() -> Settings:
    """A fully-populated Settings object that does not read the real .env file."""
    from cryptography.fernet import Fernet

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_token="test.discord.token-value-1234567890",
        config_encryption_key=Fernet.generate_key().decode(),
        torn_api_key="0123456789abcdef",
        torn_faction_id=12345,
        ffscouter_api_key="ffsc0123456789ab",
        database_url="sqlite+aiosqlite:///:memory:",
        log_level="INFO",
        log_format="console",
        mock_api=True,
    )


@pytest.fixture
async def database(settings: Settings):  # type: ignore[no-untyped-def]
    """A fresh in-memory database with the full schema created."""
    from app.database.database import Database

    db = Database(settings)
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()
