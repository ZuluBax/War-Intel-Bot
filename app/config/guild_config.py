"""Per-Discord-server configuration service.

Wraps the ``guild_configs`` table with encrypt/decrypt of the stored API keys.
The ``/setup`` command writes through here; the multi-guild war runner reads
:meth:`GuildConfigService.list_ready` each tick.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.database.database import Database
from app.database.models import GuildBoardChannel, GuildConfig
from app.services.crypto import SecretCipher
from app.utils.logging import get_logger

logger = get_logger(__name__)

_REDACTED = "•••• (set)"


@dataclass(frozen=True)
class ResolvedGuildConfig:
    """A guild's configuration with secrets decrypted, ready to build clients."""

    guild_id: int
    torn_faction_id: int
    torn_api_key: str
    ffscouter_api_key: str | None
    war_intel_role_id: int | None
    war_room_channel_id: int | None
    # (purpose, channel_id) pairs from /setup board, sorted. A tuple keeps this
    # frozen dataclass hashable and gives a stable string for the runner's signature.
    board_channels: tuple[tuple[str, int], ...] = ()


class GuildConfigService:
    def __init__(
        self,
        database: Database,
        cipher: SecretCipher,
        *,
        operator_ffscouter_key: str | None = None,
    ) -> None:
        self._db = database
        self._cipher = cipher
        self._operator_ffscouter_key = operator_ffscouter_key

    # --- writes (from /setup) --------------------------------------------
    async def set_faction(self, guild_id: int, faction_id: int, *, by: int | None) -> None:
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id) or GuildConfig(guild_id=guild_id)
            cfg.torn_faction_id = faction_id
            cfg.configured_by = by
            cfg.active = True
            session.add(cfg)
        logger.info("guild_config.faction_set", guild_id=guild_id, faction_id=faction_id)

    async def set_torn_key(self, guild_id: int, key: str, *, by: int | None) -> None:
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id) or GuildConfig(guild_id=guild_id)
            cfg.torn_api_key_enc = self._cipher.encrypt(key)
            cfg.configured_by = by
            cfg.active = True
            session.add(cfg)
        logger.info("guild_config.torn_key_set", guild_id=guild_id)

    async def set_ffscouter_key(
        self, guild_id: int, key: str | None, *, by: int | None
    ) -> None:
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id) or GuildConfig(guild_id=guild_id)
            cfg.ffscouter_api_key_enc = self._cipher.encrypt(key) if key else None
            cfg.configured_by = by
            session.add(cfg)
        logger.info("guild_config.ffscouter_key_set", guild_id=guild_id, cleared=key is None)

    async def set_role(self, guild_id: int, role_id: int | None, *, by: int | None) -> None:
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id) or GuildConfig(guild_id=guild_id)
            cfg.war_intel_role_id = role_id
            cfg.configured_by = by
            session.add(cfg)
        logger.info("guild_config.role_set", guild_id=guild_id, role_id=role_id)

    async def set_war_room_channel(
        self, guild_id: int, channel_id: int | None, *, by: int | None
    ) -> None:
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id) or GuildConfig(guild_id=guild_id)
            cfg.war_room_channel_id = channel_id
            cfg.configured_by = by
            session.add(cfg)
        logger.info("guild_config.war_room_channel_set", guild_id=guild_id, channel_id=channel_id)

    async def set_board_channel(
        self, guild_id: int, purpose: str, channel_id: int | None, *, by: int | None
    ) -> None:
        """Point one board at a channel the admins made (``None`` removes it)."""
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id) or GuildConfig(guild_id=guild_id)
            cfg.configured_by = by
            session.add(cfg)
            row = await session.scalar(
                select(GuildBoardChannel).where(
                    GuildBoardChannel.guild_id == guild_id, GuildBoardChannel.purpose == purpose
                )
            )
            if channel_id is None:
                if row is not None:
                    await session.delete(row)
            elif row is None:
                session.add(
                    GuildBoardChannel(guild_id=guild_id, purpose=purpose, channel_id=channel_id)
                )
            else:
                row.channel_id = channel_id
        logger.info(
            "guild_config.board_channel_set",
            guild_id=guild_id,
            purpose=purpose,
            channel_id=channel_id,
        )

    async def board_channels(self, guild_id: int) -> dict[str, int]:
        async with self._db.session() as session:
            rows = await session.scalars(
                select(GuildBoardChannel).where(GuildBoardChannel.guild_id == guild_id)
            )
            return {r.purpose: r.channel_id for r in rows}

    async def disable(self, guild_id: int) -> bool:
        async with self._db.session() as session:
            cfg = await session.get(GuildConfig, guild_id)
            if cfg is None:
                return False
            cfg.active = False
            session.add(cfg)
        logger.info("guild_config.disabled", guild_id=guild_id)
        return True

    # --- reads ---------------------------------------------------------
    async def _db_get(self, guild_id: int) -> GuildConfig | None:
        async with self._db.session() as session:
            return await session.get(GuildConfig, guild_id)

    async def describe(self, guild_id: int) -> dict[str, str]:
        """Human-readable view for ``/setup show`` — keys are redacted."""
        cfg = await self._db_get(guild_id)
        if cfg is None:
            return {"status": "not configured — run /setup faction and /setup torn-key"}
        return {
            "status": "active" if cfg.active else "disabled",
            "torn_faction_id": str(cfg.torn_faction_id) if cfg.torn_faction_id else "—",
            "torn_api_key": _REDACTED if cfg.torn_api_key_enc else "—",
            "ffscouter_api_key": (
                _REDACTED
                if cfg.ffscouter_api_key_enc
                else ("operator default" if self._operator_ffscouter_key else "—")
            ),
            "war_intel_role_id": str(cfg.war_intel_role_id) if cfg.war_intel_role_id else "—",
            "war_room_channel_id": (
                f"<#{cfg.war_room_channel_id}>"
                if cfg.war_room_channel_id
                else "— (auto-creates a category + 5 channels per war)"
            ),
            "board_channels": (
                ", ".join(f"{p}: <#{c}>" for p, c in sorted(boards.items()))
                if (boards := await self.board_channels(guild_id))
                else "— (none set with /setup board)"
            ),
            "ready": "yes" if cfg.is_ready else "no",
        }

    async def resolve(self, guild_id: int) -> ResolvedGuildConfig | None:
        cfg = await self._db_get(guild_id)
        if cfg is None or not cfg.is_ready:
            return None
        return self._resolved(cfg, await self.board_channels(guild_id))

    def _resolved(self, cfg: GuildConfig, boards: dict[str, int]) -> ResolvedGuildConfig:
        """Decrypt a ready config. Raises ValueError if a stored key can't be decrypted."""
        assert cfg.torn_faction_id is not None and cfg.torn_api_key_enc is not None
        return ResolvedGuildConfig(
            guild_id=cfg.guild_id,
            torn_faction_id=cfg.torn_faction_id,
            torn_api_key=self._cipher.decrypt(cfg.torn_api_key_enc),
            ffscouter_api_key=(
                self._cipher.decrypt(cfg.ffscouter_api_key_enc)
                if cfg.ffscouter_api_key_enc
                else self._operator_ffscouter_key
            ),
            war_intel_role_id=cfg.war_intel_role_id,
            war_room_channel_id=cfg.war_room_channel_id,
            board_channels=tuple(sorted(boards.items())),
        )

    def _readable(self, token: str) -> bool:
        try:
            self._cipher.decrypt(token)
        except ValueError:
            return False
        return True

    async def startup_key_check(self, *, reset_unreadable: bool) -> bool:
        """Can this encryption key read the API keys already saved? False = don't start.

        A wrong key (env var changed, or unset so a new ``config/secret.key`` was
        generated) otherwise fails quietly: every server just stops, logged only as
        ``decrypt_failed`` each tick. Refuse to start only when keys are saved and
        *none* can be read — that is a wrong key, not one corrupt row, and one bad
        server must not take the rest down.

        ``reset_unreadable`` (``RESET_UNREADABLE_KEYS``) is the way out once the
        original key is truly lost: it clears just the unreadable saved keys so the
        bot starts and those servers' admins re-run ``/setup torn-key``.
        """
        async with self._db.session() as session:
            rows = list(await session.scalars(select(GuildConfig)))
            readable = 0
            unreadable: dict[int, list[str]] = {}  # guild -> which keys
            for cfg in rows:
                for field in ("torn_api_key_enc", "ffscouter_api_key_enc"):
                    token: str | None = getattr(cfg, field)
                    if token is None:
                        continue
                    if self._readable(token):
                        readable += 1
                    else:
                        unreadable.setdefault(cfg.guild_id, []).append(field)

            if not unreadable:
                return True

            if reset_unreadable:
                for cfg in rows:
                    for field in unreadable.get(cfg.guild_id, []):
                        setattr(cfg, field, None)
                logger.warning(
                    "crypto.unreadable_keys_cleared",
                    guild_ids=sorted(unreadable),
                    note="these servers' admins must re-run /setup torn-key (and "
                    "/setup ffscouter-key if they had one); remove "
                    "RESET_UNREADABLE_KEYS now",
                )
                return True

        if readable == 0:
            logger.error(
                "crypto.key_mismatch",
                unreadable_servers=len(unreadable),
                note="the encryption key cannot read ANY saved API key, so it is not "
                "the key they were saved with. Set CONFIG_ENCRYPTION_KEY back to the "
                "original key (and on Railway check the variable is still set). Only "
                "if that key is lost for good: start once with "
                "RESET_UNREADABLE_KEYS=true, then every server re-runs /setup torn-key.",
            )
            return False

        logger.error(
            "crypto.some_keys_unreadable",
            guild_ids=sorted(unreadable),
            note="these servers are skipped until their admins re-run /setup torn-key "
            "(or start once with RESET_UNREADABLE_KEYS=true to clear them)",
        )
        return True

    async def war_intel_role_id(self, guild_id: int) -> int | None:
        cfg = await self._db_get(guild_id)
        return cfg.war_intel_role_id if cfg else None

    async def list_ready(self) -> list[ResolvedGuildConfig]:
        async with self._db.session() as session:
            rows = list(
                await session.scalars(select(GuildConfig).where(GuildConfig.active.is_(True)))
            )
            boards: dict[int, dict[str, int]] = {}
            for b in await session.scalars(select(GuildBoardChannel)):
                boards.setdefault(b.guild_id, {})[b.purpose] = b.channel_id
        resolved: list[ResolvedGuildConfig] = []
        for cfg in rows:
            if not cfg.is_ready:
                continue
            try:
                resolved.append(self._resolved(cfg, boards.get(cfg.guild_id, {})))
            except ValueError:
                logger.error("guild_config.decrypt_failed", guild_id=cfg.guild_id)
        return resolved
