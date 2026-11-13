from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings

_BASE = dict(
    discord_token="test.discord.token-value-1234567890",
    torn_api_key="0123456789abcdef",
    torn_faction_id=99,
    ffscouter_api_key="ffsc0123456789ab",
)


def _make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **overrides})  # type: ignore[arg-type,call-arg]


def test_rejects_sync_database_url() -> None:
    with pytest.raises(ValidationError):
        _make(database_url="sqlite:///./x.db")
    with pytest.raises(ValidationError):
        _make(database_url="postgresql://u:p@h/db")


def test_accepts_async_drivers() -> None:
    assert _make(database_url="sqlite+aiosqlite:///./x.db").is_sqlite is True
    assert _make(database_url="postgresql+asyncpg://u:p@h/db").is_sqlite is False


def test_secrets_are_masked_in_repr() -> None:
    settings = _make()
    text = repr(settings)
    assert "0123456789abcdef" not in text
    assert "ffsc0123456789ab" not in text


def test_blank_env_values_are_treated_as_unset() -> None:
    # A freshly-copied .env.example has these keys present but empty.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        discord_token="",
        discord_dev_guild_id="",
        torn_api_key="0123456789abcdef",
        torn_faction_id="  ",
        ffscouter_api_key="",
    )
    assert settings.discord_token is None
    assert settings.discord_dev_guild_id is None
    assert settings.torn_faction_id is None
    assert settings.ffscouter_api_key is None


def test_missing_for_run_needs_only_the_bot_token() -> None:
    # The bot is multi-tenant: Torn / FFScouter keys are per-server via /setup.
    assert Settings(_env_file=None).missing_for_run() == ["DISCORD_TOKEN"]  # type: ignore[call-arg]
    ready = Settings(_env_file=None, discord_token="x.y.z")  # type: ignore[call-arg]
    assert ready.missing_for_run() == []
