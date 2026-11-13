"""In-memory GuildGateway for tests — no live Discord."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from types import SimpleNamespace

import discord

from app.discord.gateway import GatewayNotReady


class FakeGuildGateway:
    def __init__(self, *, ready: bool = True, manage_channels: bool = True) -> None:
        self._ids = itertools.count(1000)
        self._ready = ready
        self.manage_channels = manage_channels
        # Simulates Discord refusing a create even though the permission looks held
        # (e.g. a category overwrite denies it).
        self.refuse_creates = False
        self.create_attempts = 0
        self.categories: dict[int, str] = {}
        self.channels: dict[int, dict[str, object]] = {}
        # (channel_id, message_id) -> embeds
        self.messages: dict[tuple[int, int], list[discord.Embed]] = {}
        self.deleted: set[tuple[int, int]] = set()
        # Messages the bot itself deleted (remove_message), as opposed to by hand.
        self.removed: list[tuple[int, int]] = []
        # (channel_id, message_id) -> [(filename, content)] for messages sent with files
        self.attachments: dict[tuple[int, int], list[tuple[str, bytes]]] = {}
        # (channel_id, message_id) -> the buttons / drop-downs last set on that message
        self.views: dict[tuple[int, int], discord.ui.View] = {}
        # One entry is consumed per send_embeds call: an exception is raised, None
        # lets that send through. Empty list = every send succeeds.
        self.send_failures: list[Exception | None] = []

    def _new_id(self) -> int:
        return next(self._ids)

    def _check_ready(self) -> None:
        if not self._ready:
            raise GatewayNotReady("fake gateway not ready")

    async def category_exists(self, category_id: int) -> bool:
        self._check_ready()
        return category_id in self.categories

    async def channel_exists(self, channel_id: int) -> bool:
        self._check_ready()
        return channel_id in self.channels

    async def can_manage_channels(self) -> bool:
        self._check_ready()
        return self.manage_channels

    def _check_create(self) -> None:
        self.create_attempts += 1
        if self.refuse_creates or not self.manage_channels:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
            )

    async def create_category(self, name: str) -> int:
        self._check_ready()
        self._check_create()
        cid = self._new_id()
        self.categories[cid] = name
        return cid

    async def rename_category(self, category_id: int, name: str) -> None:
        self._check_ready()
        if category_id in self.categories:
            self.categories[category_id] = name

    async def create_text_channel(self, name: str, *, category_id: int, topic: str) -> int:
        self._check_ready()
        self._check_create()
        cid = self._new_id()
        self.channels[cid] = {"name": name, "category_id": category_id, "topic": topic}
        return cid

    async def send_embeds(
        self,
        channel_id: int,
        embeds: list[discord.Embed],
        *,
        attachments: Sequence[tuple[str, bytes]] = (),
        view: discord.ui.View | None = None,
    ) -> int:
        self._check_ready()
        if self.send_failures:
            failure = self.send_failures.pop(0)
            if failure is not None:
                raise failure
        mid = self._new_id()
        self.messages[(channel_id, mid)] = embeds
        if attachments:
            self.attachments[(channel_id, mid)] = list(attachments)
        if view is not None:
            self.views[(channel_id, mid)] = view
        return mid

    async def edit_embeds(
        self,
        channel_id: int,
        message_id: int,
        embeds: list[discord.Embed],
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        self._check_ready()
        key = (channel_id, message_id)
        if key in self.messages and key not in self.deleted:
            self.messages[key] = embeds
            if view is not None:
                self.views[key] = view
            return True
        return False

    async def remove_message(self, channel_id: int, message_id: int) -> None:
        self._check_ready()
        self.removed.append((channel_id, message_id))
        self.deleted.add((channel_id, message_id))

    # --- test helpers ---------------------------------------------------
    def delete_channel(self, channel_id: int) -> None:
        self.channels.pop(channel_id, None)

    def delete_message(self, channel_id: int, message_id: int) -> None:
        self.deleted.add((channel_id, message_id))

    def message_count(self, channel_id: int) -> int:
        return sum(
            1 for (cid, mid) in self.messages if cid == channel_id and (cid, mid) not in self.deleted
        )


def discord_http_error(cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    """A discord.py HTTP error as the library raises it (it reads status/reason)."""
    return cls(SimpleNamespace(status=status, reason="test"), "simulated")
