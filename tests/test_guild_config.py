from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config.guild_config import GuildConfigService
from app.database.database import Database
from app.database.models import GuildConfig
from app.services.crypto import SecretCipher

GUILD = 123456789


@pytest.fixture
def service(database: Database) -> GuildConfigService:
    return GuildConfigService(
        database, SecretCipher(Fernet.generate_key()), operator_ffscouter_key="op-ffs"
    )


async def test_not_ready_until_faction_and_torn_key_set(
    database: Database, service: GuildConfigService
) -> None:
    assert await service.resolve(GUILD) is None
    assert await service.list_ready() == []

    await service.set_faction(GUILD, 10001, by=1)
    assert await service.resolve(GUILD) is None  # still no key

    await service.set_torn_key(GUILD, "torn-secret", by=1)
    resolved = await service.resolve(GUILD)
    assert resolved is not None
    assert resolved.torn_faction_id == 10001
    assert resolved.torn_api_key == "torn-secret"
    assert resolved.ffscouter_api_key == "op-ffs"  # falls back to operator key


async def test_keys_are_stored_encrypted(
    database: Database, service: GuildConfigService
) -> None:
    await service.set_torn_key(GUILD, "plaintext-key", by=1)
    async with database.session() as session:
        row = await session.scalar(select(GuildConfig).where(GuildConfig.guild_id == GUILD))
    assert row is not None
    assert row.torn_api_key_enc is not None
    assert "plaintext-key" not in row.torn_api_key_enc


async def test_own_ffscouter_key_overrides_operator_default(
    database: Database, service: GuildConfigService
) -> None:
    await service.set_faction(GUILD, 1, by=1)
    await service.set_torn_key(GUILD, "t", by=1)
    await service.set_ffscouter_key(GUILD, "guild-ffs", by=1)
    resolved = await service.resolve(GUILD)
    assert resolved is not None and resolved.ffscouter_api_key == "guild-ffs"

    await service.set_ffscouter_key(GUILD, None, by=1)  # clear
    resolved = await service.resolve(GUILD)
    assert resolved is not None and resolved.ffscouter_api_key == "op-ffs"


async def test_disable_removes_from_list_ready(
    database: Database, service: GuildConfigService
) -> None:
    await service.set_faction(GUILD, 1, by=1)
    await service.set_torn_key(GUILD, "t", by=1)
    assert [c.guild_id for c in await service.list_ready()] == [GUILD]

    assert await service.disable(GUILD) is True
    assert await service.list_ready() == []
    assert await service.resolve(GUILD) is None

    # re-running setup re-activates
    await service.set_faction(GUILD, 1, by=1)
    assert [c.guild_id for c in await service.list_ready()] == [GUILD]


async def test_describe_redacts_keys(database: Database, service: GuildConfigService) -> None:
    await service.set_faction(GUILD, 999, by=1)
    await service.set_torn_key(GUILD, "super-secret", by=1)
    info = await service.describe(GUILD)
    assert "super-secret" not in str(info)
    assert info["torn_faction_id"] == "999"
    assert info["ready"] == "yes"


async def test_describe_for_unconfigured_guild(
    database: Database, service: GuildConfigService
) -> None:
    info = await service.describe(GUILD)
    assert "not configured" in info["status"]


async def test_war_room_channel_defaults_to_none(
    database: Database, service: GuildConfigService
) -> None:
    await service.set_faction(GUILD, 1, by=1)
    await service.set_torn_key(GUILD, "t", by=1)
    resolved = await service.resolve(GUILD)
    assert resolved is not None
    assert resolved.war_room_channel_id is None
    assert "auto-creates" in (await service.describe(GUILD))["war_room_channel_id"]


async def test_set_war_room_channel_and_clear(
    database: Database, service: GuildConfigService
) -> None:
    await service.set_faction(GUILD, 1, by=1)
    await service.set_torn_key(GUILD, "t", by=1)

    await service.set_war_room_channel(GUILD, 999888777, by=1)
    resolved = await service.resolve(GUILD)
    assert resolved is not None and resolved.war_room_channel_id == 999888777
    assert (await service.describe(GUILD))["war_room_channel_id"] == "<#999888777>"

    await service.set_war_room_channel(GUILD, None, by=1)  # clear
    resolved = await service.resolve(GUILD)
    assert resolved is not None and resolved.war_room_channel_id is None


async def test_board_channels_set_replace_and_remove(service: GuildConfigService) -> None:
    await service.set_faction(GUILD, 10001, by=1)
    await service.set_torn_key(GUILD, "torn-secret", by=1)

    await service.set_board_channel(GUILD, "information", 111, by=1)
    await service.set_board_channel(GUILD, "log", 222, by=1)
    await service.set_board_channel(GUILD, "log", 333, by=1)  # moved, not duplicated
    assert await service.board_channels(GUILD) == {"information": 111, "log": 333}

    resolved = await service.resolve(GUILD)
    assert resolved is not None
    assert resolved.board_channels == (("information", 111), ("log", 333))
    [listed] = await service.list_ready()
    assert listed.board_channels == resolved.board_channels

    await service.set_board_channel(GUILD, "information", None, by=1)
    assert await service.board_channels(GUILD) == {"log": 333}
    assert "log: <#333>" in (await service.describe(GUILD))["board_channels"]


async def test_board_channels_are_per_server(service: GuildConfigService) -> None:
    await service.set_board_channel(GUILD, "stats", 111, by=1)
    await service.set_board_channel(GUILD + 1, "stats", 222, by=1)
    assert await service.board_channels(GUILD) == {"stats": 111}
    assert await service.board_channels(GUILD + 1) == {"stats": 222}


# --- startup check: is the encryption key the one the saved keys were made with? --
# A wrong or newly generated key used to fail quietly: every server's boards just
# stopped, with only a decrypt_failed log line per server per tick.


def _with_other_key(database: Database) -> GuildConfigService:
    return GuildConfigService(database, SecretCipher(Fernet.generate_key()))


async def _save_keys(service: GuildConfigService, guild_id: int) -> None:
    await service.set_faction(guild_id, 10001, by=1)
    await service.set_torn_key(guild_id, f"torn-{guild_id}", by=1)
    await service.set_ffscouter_key(guild_id, f"ffs-{guild_id}", by=1)


async def test_startup_allowed_on_a_fresh_install(service: GuildConfigService) -> None:
    assert await service.startup_key_check(reset_unreadable=False) is True


async def test_startup_allowed_when_the_key_reads_the_saved_keys(
    service: GuildConfigService,
) -> None:
    await _save_keys(service, GUILD)
    assert await service.startup_key_check(reset_unreadable=False) is True


async def test_startup_refused_when_no_saved_key_can_be_read(
    database: Database, service: GuildConfigService
) -> None:
    await _save_keys(service, GUILD)
    await _save_keys(service, GUILD + 1)

    wrong = _with_other_key(database)
    assert await wrong.startup_key_check(reset_unreadable=False) is False
    # Refusing must not touch anything: the right key still works afterwards.
    assert (await service.resolve(GUILD)) is not None
    assert (await service.resolve(GUILD + 1)) is not None


async def test_one_unreadable_server_does_not_stop_the_others(
    database: Database, service: GuildConfigService
) -> None:
    await _save_keys(service, GUILD)
    await _save_keys(service, GUILD + 1)
    async with database.session() as session:
        row = await session.get(GuildConfig, GUILD + 1)
        assert row is not None
        row.torn_api_key_enc = "corrupt"

    assert await service.startup_key_check(reset_unreadable=False) is True
    assert [c.guild_id for c in await service.list_ready()] == [GUILD]


async def test_reset_clears_only_unreadable_keys_and_lets_the_bot_start(
    database: Database, service: GuildConfigService
) -> None:
    await _save_keys(service, GUILD)
    await service.set_role(GUILD, 777, by=1)

    new_key = _with_other_key(database)  # the original key is lost for good
    assert await new_key.startup_key_check(reset_unreadable=True) is True

    async with database.session() as session:
        row = await session.get(GuildConfig, GUILD)
    assert row is not None
    assert row.torn_api_key_enc is None
    assert row.ffscouter_api_key_enc is None
    assert row.torn_faction_id == 10001  # everything else is kept
    assert row.war_intel_role_id == 777
    assert await new_key.resolve(GUILD) is None  # not ready until /setup torn-key again

    await new_key.set_torn_key(GUILD, "torn-new", by=1)
    resolved = await new_key.resolve(GUILD)
    assert resolved is not None and resolved.torn_api_key == "torn-new"


async def test_reset_leaves_readable_keys_alone(
    database: Database, service: GuildConfigService
) -> None:
    await _save_keys(service, GUILD)
    assert await service.startup_key_check(reset_unreadable=True) is True
    resolved = await service.resolve(GUILD)
    assert resolved is not None and resolved.torn_api_key == f"torn-{GUILD}"
