"""Central application configuration.

All configuration enters the application here and nowhere else. Nothing in the
codebase should read ``os.environ`` directly — inject a :class:`Settings`
instance instead so tests can supply their own values and secrets never leak
into module-level globals.

The external credentials (Discord token, Discord guild, Torn key + faction,
FFScouter key) are all *optional at load time* and default to ``None``. This
lets diagnostic scripts run with only the config they need — e.g. probing the
Torn API before a Discord token exists. :meth:`Settings.missing_for_run`
enforces the full set, and ``app.main`` calls it before starting the bot.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["console", "json"]

# env-var names the bot cannot start without (checked by missing_for_run()).
# The bot is multi-tenant: per-server Torn / FFScouter keys and faction IDs are
# set in Discord via /setup and stored (encrypted) in the database, so they are
# NOT required here.
_REQUIRED_FOR_RUN = ("DISCORD_TOKEN",)


class Settings(BaseSettings):
    """Typed, validated view of the process environment / ``.env`` file.

    Secrets are stored as :class:`~pydantic.SecretStr` so an accidental
    ``repr()``/log of the settings object prints ``**********`` rather than the
    real value. Call ``.get_secret_value()`` only at the point of use.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Discord ---------------------------------------------------------
    discord_token: SecretStr | None = Field(default=None, description="Discord bot token.")
    # Optional: a "home"/dev server. Commands are also synced globally and
    # instantly to each server on join, but a global sync can take up to an
    # hour to propagate — set this to get instant sync while developing.
    discord_dev_guild_id: int | None = Field(
        default=None, description="Optional server for instant command sync during development."
    )

    # --- Secrets encryption -------------------------------------------------
    # urlsafe-base64 32-byte Fernet key for encrypting per-guild API keys at
    # rest. If unset, app.services.crypto falls back to a generated
    # config/secret.key file.
    config_encryption_key: SecretStr | None = Field(default=None)
    # Emergency only, for one start: the original encryption key is lost for good.
    # Clears the saved API keys the current key cannot read (everything else is
    # kept) so the bot can start; those servers re-run /setup torn-key. Without it
    # the bot refuses to start when no saved key can be read (see app.main).
    reset_unreadable_keys: bool = Field(default=False)

    # --- Torn / FFScouter API (operator defaults / local dev only) --------
    # Per-server keys from /setup take precedence. These are an optional
    # fallback used only when a server has not supplied its own.
    torn_api_key: SecretStr | None = Field(default=None, description="Operator Torn API key.")
    torn_faction_id: int | None = Field(default=None, description="Operator/dev Torn faction ID.")
    torn_api_base_url: str = Field(default="https://api.torn.com/v2")
    ffscouter_api_key: SecretStr | None = Field(
        default=None, description="Operator FFScouter API key (fallback)."
    )
    ffscouter_api_base_url: str = Field(default="https://ffscouter.com/api/v1")

    # --- Database ------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/torn_war_intelligence.db",
        description="SQLAlchemy async URL. Use postgresql+asyncpg://... in prod.",
    )

    # --- Logging -----------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    # --- Scheduling (seconds) ---------------------------------------------
    war_check_interval_seconds: int = Field(default=60, ge=10)
    war_check_interval_fast_seconds: int = Field(default=15, ge=10)
    # 15 s so the target board shows enemies healing out of hospital (app.war.hospital_watch)
    # almost as it happens. Floor 15: each refresh is 1 Torn call per server plus ~50
    # database writes, and must finish before the next one starts.
    status_refresh_interval_seconds: int = Field(default=15, ge=15)
    stats_refresh_interval_seconds: int = Field(default=900, ge=60)

    # --- Behaviour toggles ----------------------------------------------
    mock_api: bool = Field(
        default=False,
        description="Serve Torn/FFScouter from local fixtures. Never true in prod.",
    )

    # --- Health check --------------------------------------------------
    # A tiny inbound HTTP endpoint the bot otherwise has no reason to expose.
    # Needed for host platforms that decide "is this thing alive?" by probing
    # a port rather than watching the process (e.g. Cloudflare Containers'
    # startAndWaitForPorts()/pingEndpoint, or a Docker HEALTHCHECK).
    health_port: int = Field(default=8080, ge=1, le=65535)

    @field_validator(
        "discord_token",
        "discord_dev_guild_id",
        "config_encryption_key",
        "torn_api_key",
        "torn_faction_id",
        "ffscouter_api_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat ``VAR=`` (empty / whitespace) in ``.env`` as "not set" -> None.

        A freshly-copied ``.env.example`` has these keys present but blank;
        without this every blank line would be a hard validation error.
        """
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Guard against a sync driver URL that would deadlock the event loop."""
        allowed_prefixes = ("sqlite+aiosqlite:", "postgresql+asyncpg:")
        if not value.startswith(allowed_prefixes):
            raise ValueError(
                "DATABASE_URL must use an async driver: "
                "sqlite+aiosqlite://... or postgresql+asyncpg://..."
            )
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def operator_ffscouter_key(self) -> str | None:
        return self.ffscouter_api_key.get_secret_value() if self.ffscouter_api_key else None

    def missing_for_run(self) -> list[str]:
        """Return the env-var names still needed before the bot can start."""
        present = {"DISCORD_TOKEN": self.discord_token is not None}
        return [name for name in _REQUIRED_FOR_RUN if not present[name]]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once. Tests that need a different
    configuration should construct :class:`Settings` directly (optionally via
    ``Settings(_env_file=None, ...)``) rather than calling this.
    """
    return Settings()  # values come from the environment / .env
