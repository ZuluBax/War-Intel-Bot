from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from app.config.guild_config import GuildConfigService
from app.config.settings import Settings
from app.database.database import Database
from app.services.crypto import SecretCipher
from app.utils.time import utcnow
from app.war.multi_guild import MultiGuildWarRunner, RefreshOutcome
from tests.fake_gateway import FakeGuildGateway

TORN = "https://api.torn.com/v2"


@pytest.fixture
def guild_config(database: Database) -> GuildConfigService:
    return GuildConfigService(database, SecretCipher(Fernet.generate_key()))


def _runner(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> MultiGuildWarRunner:
    return MultiGuildWarRunner(
        database, guild_config, settings, make_gateway=lambda _gid: FakeGuildGateway()
    )


async def _configure(gc: GuildConfigService, guild_id: int, faction_id: int) -> None:
    await gc.set_faction(guild_id, faction_id, by=1)
    await gc.set_torn_key(guild_id, f"key-{guild_id}", by=1)


async def test_manager_is_cached_and_rebuilt_on_config_change(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)
    runner = _runner(database, guild_config, settings)

    cfg = (await guild_config.list_ready())[0]
    m1 = runner._manager_for(cfg)
    m2 = runner._manager_for(cfg)
    assert m1 is m2  # cached

    await guild_config.set_faction(1, 200, by=1)  # config changed
    cfg2 = (await guild_config.list_ready())[0]
    m3 = runner._manager_for(cfg2)
    assert m3 is not m1

    await runner.aclose()


@respx.mock
async def test_tick_runs_every_ready_guild_and_isolates_failures(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)  # this faction's call succeeds
    await _configure(guild_config, 2, 200)  # this faction's call errors

    respx.get(f"{TORN}/faction/100/rankedwars").mock(
        return_value=httpx.Response(200, json={"rankedwars": []})
    )
    respx.get(f"{TORN}/faction/200/rankedwars").mock(
        return_value=httpx.Response(200, json={"error": {"code": 8, "error": "IP ban"}})
    )

    runner = _runner(database, guild_config, settings)
    await runner.tick()  # must not raise despite guild 2 failing

    # both managers were built
    assert set(runner._cache) == {1, 2}
    await runner.aclose()


async def test_tick_drops_manager_for_disabled_guild(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)
    runner = _runner(database, guild_config, settings)

    with respx.mock:
        respx.get(f"{TORN}/faction/100/rankedwars").mock(
            return_value=httpx.Response(200, json={"rankedwars": []})
        )
        await runner.tick()
    assert 1 in runner._cache

    await guild_config.disable(1)
    await runner.tick()
    assert runner._cache == {}
    await runner.aclose()


# --- refresh_tick / manual_refresh (Phase 5) --------------------------------


async def test_refresh_tick_runs_and_isolates_failures_with_no_active_wars(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)
    await _configure(guild_config, 2, 200)
    runner = _runner(database, guild_config, settings)

    await runner.refresh_tick(include_stats=False)  # no active war for either -> no-ops cleanly

    assert set(runner._cache) == {1, 2}
    await runner.aclose()


async def test_manual_refresh_not_configured(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    runner = _runner(database, guild_config, settings)
    assert await runner.manual_refresh(999) is RefreshOutcome.NOT_CONFIGURED
    await runner.aclose()


async def test_manual_refresh_no_active_war(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)
    runner = _runner(database, guild_config, settings)
    assert await runner.manual_refresh(1) is RefreshOutcome.NO_ACTIVE_WAR
    await runner.aclose()


async def test_manual_refresh_is_rate_limited_on_immediate_retry(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)
    runner = _runner(database, guild_config, settings)

    first = await runner.manual_refresh(1)
    second = await runner.manual_refresh(1)

    assert first is RefreshOutcome.NO_ACTIVE_WAR  # consumed the cooldown slot regardless
    assert second is RefreshOutcome.RATE_LIMITED
    await runner.aclose()


async def test_manual_refresh_allowed_again_after_cooldown_elapses(
    database: Database, guild_config: GuildConfigService, settings: Settings
) -> None:
    await _configure(guild_config, 1, 100)
    runner = _runner(database, guild_config, settings)

    await runner.manual_refresh(1)
    runner._last_manual_refresh[1] = utcnow() - timedelta(
        seconds=settings.status_refresh_interval_seconds + 1
    )
    assert await runner.manual_refresh(1) is RefreshOutcome.NO_ACTIVE_WAR  # not rate-limited
    await runner.aclose()


# --- one job at a time per server --------------------------------------------


class _OverlapRecorder:
    """Stands in for a WarManager; records how many jobs were inside it at once."""

    def __init__(self, guild_id: int, active: dict[int, int], peaks: dict[str, int]) -> None:
        self._gid = guild_id
        self._active = active
        self._peaks = peaks
        self.calls = 0

    async def _work(self) -> None:
        self.calls += 1
        self._active[self._gid] += 1
        self._peaks["same_server"] = max(self._peaks["same_server"], self._active[self._gid])
        self._peaks["all_servers"] = max(self._peaks["all_servers"], sum(self._active.values()))
        await asyncio.sleep(0.02)  # long enough for the other jobs to try to get in
        self._active[self._gid] -= 1

    async def tick(self) -> None:
        await self._work()

    async def refresh_active(self, *, include_stats: bool = True) -> None:
        await self._work()


async def test_jobs_never_overlap_on_one_server_but_servers_run_in_parallel(
    database: Database,
    guild_config: GuildConfigService,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The war-detect, status, stats and /refresh paths all touch the same war's rows;
    # run together they double-post roster events and duplicate target rankings.
    await _configure(guild_config, 1, 100)
    await _configure(guild_config, 2, 200)
    runner = _runner(database, guild_config, settings)

    active = {1: 0, 2: 0}
    peaks = {"same_server": 0, "all_servers": 0}
    managers = {gid: _OverlapRecorder(gid, active, peaks) for gid in (1, 2)}
    monkeypatch.setattr(runner, "_manager_for", lambda cfg: managers[cfg.guild_id])

    await asyncio.gather(
        runner.tick(),
        runner.refresh_tick(include_stats=False),
        runner.refresh_tick(include_stats=True),
        runner.manual_refresh(1),
    )

    assert peaks["same_server"] == 1  # never two jobs inside one server's war at once
    assert peaks["all_servers"] >= 2  # but one server doesn't make the others wait
    await runner.aclose()


async def test_status_refresh_skips_a_busy_server_instead_of_queueing(
    database: Database,
    guild_config: GuildConfigService,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _configure(guild_config, 1, 100)
    await _configure(guild_config, 2, 200)
    runner = _runner(database, guild_config, settings)

    active = {1: 0, 2: 0}
    peaks = {"same_server": 0, "all_servers": 0}
    managers = {gid: _OverlapRecorder(gid, active, peaks) for gid in (1, 2)}
    monkeypatch.setattr(runner, "_manager_for", lambda cfg: managers[cfg.guild_id])

    async with runner._lock_for(1):  # server 1 is mid-job (e.g. a slow stats pass)
        await runner.refresh_tick(include_stats=False)  # must return, not wait

    assert managers[1].calls == 0  # busy server skipped this round
    assert managers[2].calls == 1  # the others still refreshed
    await runner.aclose()
