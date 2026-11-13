"""SQLAlchemy ORM models — the persistent schema.

Design notes
------------
* Every table uses an internal integer primary key. Torn's own identifiers
  (faction ID, player ID, war ID) are stored as separate, indexed columns so
  they remain stable external references even if we re-key internally.
* ``stat_estimates`` is append-only: every FFScouter response is a new row.
  Trend / confidence are *derived* from the history, never stored destructively,
  and a failed refresh must never delete the last good row (spec §27).
* ``confirmed_stats`` (manual spy reports, spec §24) is deliberately a separate
  table from ``stat_estimates``. Estimated and confirmed numbers are never mixed.
* Timestamps are timezone-aware UTC (see ``app.utils.time``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that guarantees a tz-aware UTC value back out.

    SQLite (dev/tests) has no native timezone-aware datetime type — aiosqlite
    always hands back a naive ``datetime`` regardless of ``timezone=True`` on
    the column, which silently breaks the "timestamps are timezone-aware UTC"
    invariant the whole codebase relies on (``app.utils.time``). Postgres
    (prod, via asyncpg) already round-trips tz-aware values correctly, so this
    is a no-op there.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    """Declarative base for all models."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GuildConfig(Base, TimestampMixin):
    """Per-Discord-server configuration, written by the ``/setup`` command.

    The bot is multi-tenant: one process serves many factions' servers. Each
    server records which Torn faction it represents and supplies its own Torn
    (and optionally FFScouter) API key. Keys are stored **encrypted**
    (``app.services.crypto.SecretCipher``); this table never holds clear text.
    """

    __tablename__ = "guild_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    torn_faction_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    torn_api_key_enc: Mapped[str | None] = mapped_column(String(512))
    ffscouter_api_key_enc: Mapped[str | None] = mapped_column(String(512))
    war_intel_role_id: Mapped[int | None] = mapped_column(BigInteger)
    # If set, every war purpose (info/stats/targets/log) posts into this
    # one existing channel instead of the bot creating its own category + 4
    # channels per war (spec §37 alternative layout, added on user request).
    war_room_channel_id: Mapped[int | None] = mapped_column(BigInteger)

    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    configured_by: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def is_ready(self) -> bool:
        """True once the minimum needed to run war detection is set."""
        return (
            self.active
            and self.torn_faction_id is not None
            and self.torn_api_key_enc is not None
        )


class GuildBoardChannel(Base, TimestampMixin):
    """A channel the server's admins made themselves for one board (``/setup board``).

    Lets a server run the bot without Manage Channels: when a server has any rows
    here, the bot never creates a category or channels for it, it only posts into
    these. One row per (server, purpose); purposes match ``CHANNEL_LAYOUT``.
    """

    __tablename__ = "guild_board_channels"
    __table_args__ = (
        UniqueConstraint("guild_id", "purpose", name="uq_guild_board_channels_guild_purpose"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Faction(Base, TimestampMixin):
    __tablename__ = "factions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    torn_faction_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    is_ours: Mapped[bool] = mapped_column(default=False, nullable=False)

    wars_as_ours: Mapped[list[War]] = relationship(
        back_populates="our_faction", foreign_keys="War.our_faction_id"
    )


class War(Base, TimestampMixin):
    __tablename__ = "wars"
    __table_args__ = (
        # One row per Torn ranked war *per Discord server* — the idempotency
        # backbone (spec §30). Two servers can each track the same Torn war.
        UniqueConstraint("guild_id", "torn_war_id", name="uq_wars_guild_torn_war_id"),
        Index("ix_wars_status", "status"),
        Index("ix_wars_guild", "guild_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    torn_war_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    our_faction_id: Mapped[int] = mapped_column(ForeignKey("factions.id"))
    our_faction_name: Mapped[str | None] = mapped_column(String(128))
    enemy_faction_id: Mapped[int] = mapped_column(ForeignKey("factions.id"))
    enemy_faction_name: Mapped[str | None] = mapped_column(String(128))

    # NO_WAR / WAR_DETECTED / WAR_ACTIVE / WAR_FINISHED (see app.war.detector).
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str | None] = mapped_column(String(16))  # WIN / LOSS / DRAW / None

    war_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    war_end: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Discord workspace IDs — populated once, reused on every refresh.
    discord_category_id: Mapped[int | None] = mapped_column(BigInteger)
    discord_information_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    discord_stats_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    discord_targets_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    discord_log_channel_id: Mapped[int | None] = mapped_column(BigInteger)

    our_faction: Mapped[Faction] = relationship(
        back_populates="wars_as_ours", foreign_keys=[our_faction_id]
    )
    participants: Mapped[list[WarPlayer]] = relationship(
        back_populates="war", cascade="all, delete-orphan"
    )
    channel_messages: Mapped[list[DiscordChannel]] = relationship(
        back_populates="war", cascade="all, delete-orphan"
    )
    log_events: Mapped[list[WarLogEvent]] = relationship(
        back_populates="war", cascade="all, delete-orphan"
    )
    hits: Mapped[list[WarHit]] = relationship(
        back_populates="war", cascade="all, delete-orphan"
    )


class Player(Base, TimestampMixin):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    torn_player_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(64))
    level: Mapped[int | None] = mapped_column(Integer)
    last_seen_faction_id: Mapped[int | None] = mapped_column(BigInteger)

    estimates: Mapped[list[StatEstimate]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    status_history: Mapped[list[PlayerStatusHistory]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    confirmed_stats: Mapped[list[ConfirmedStat]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )


class WarPlayer(Base, TimestampMixin):
    """Association of a player with a specific war (the enemy roster)."""

    __tablename__ = "war_players"
    __table_args__ = (
        UniqueConstraint("war_id", "player_id", name="uq_war_players_war_player"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False, default="ENEMY")  # ENEMY / OURS
    left_faction: Mapped[bool] = mapped_column(default=False, nullable=False)

    # When the player's *current* status description first appeared, so a flight's
    # take-off time is known. ``status_since`` is only set when the change was seen
    # happening (the previous look was recent); NULL means "already like this when we
    # first looked" — e.g. mid-flight — and no landing time can be worked out.
    status_description: Mapped[str | None] = mapped_column(String(128))
    status_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    war: Mapped[War] = relationship(back_populates="participants")
    player: Mapped[Player] = relationship()


class StatEstimate(Base):
    """One FFScouter (or future in-house) battle-stat estimate. Append-only."""

    __tablename__ = "stat_estimates"
    __table_args__ = (
        Index("ix_stat_estimates_player_time", "player_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)

    estimated_bs: Mapped[float | None] = mapped_column(Float)
    estimated_bs_human: Mapped[str | None] = mapped_column(String(16))
    bss_public: Mapped[float | None] = mapped_column(Float)
    fair_fight: Mapped[float | None] = mapped_column(Float)

    source: Mapped[str] = mapped_column(String(32), nullable=False, default="ffscouter")
    # FFScouter's own "last_updated" for the estimate (may be older than our fetch).
    estimate_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When *we* recorded the row.
    observed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    player: Mapped[Player] = relationship(back_populates="estimates")


class FlightRecord(Base):
    """A flight the bot watched from take-off to landing. Append-only.

    Lets the bot learn which ticket each player flies an airliner on (BCT is ~3x faster
    than a normal ticket and Torn shows both as "airliner"), and gives real flights to
    check the flight-time table in ``app.war.live_status`` against.
    """

    __tablename__ = "flight_records"
    __table_args__ = (Index("ix_flight_records_player_landed", "player_id", "landed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    place: Mapped[str] = mapped_column(String(64), nullable=False)  # the foreign country
    departed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # Halfway between the last "still flying" look and the first look after landing.
    landed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    plane_type: Mapped[str | None] = mapped_column(String(32))  # Torn's, while flying
    # Worked out from the duration (app.war.live_status.classify_flight).
    method: Mapped[str | None] = mapped_column(String(16))


class Overdose(Base):
    """An enemy overdose seen during a war (from the hospital reason, app.war.overdose).
    One row per hospital stay: ``hospital_until`` follows Torn's release time while the
    stay lasts, so a stay cut short by meds still ends the episode."""

    __tablename__ = "overdoses"
    __table_args__ = (Index("ix_overdoses_war_player", "war_id", "player_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    drug: Mapped[str] = mapped_column(String(32), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    hospital_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Already in hospital for it the first time the bot looked at this player in this
    # war, i.e. it happened before the war was announced: kept off the board and log.
    before_watch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PlayerActivity(Base):
    """A 5-minute slot in which a player did something in Torn (from Torn's
    ``last_action`` timestamp). One row per player per active slot — the same unit
    FFScouter's premium Activity Tracker uses. Kept across wars: an enemy we have
    fought before arrives with history."""

    __tablename__ = "player_activity"
    __table_args__ = (
        UniqueConstraint("player_id", "slot_start", name="uq_player_activity_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    slot_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class ActivityWatch(Base):
    """A 5-minute slot in which the bot was watching a war's enemy. Activity is only
    judged over watched slots, so time the bot was down never reads as "quiet"."""

    __tablename__ = "activity_watch"
    __table_args__ = (UniqueConstraint("war_id", "slot_start", name="uq_activity_watch_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    slot_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class PlayerStatusHistory(Base):
    """Point-in-time snapshot of a player's Torn status during a war."""

    __tablename__ = "player_status_history"
    __table_args__ = (
        Index("ix_status_history_player_time", "player_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    war_id: Mapped[int | None] = mapped_column(ForeignKey("wars.id"), index=True)

    # OKAY / HOSPITAL / JAIL / TRAVELING / UNKNOWN
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    description: Mapped[str | None] = mapped_column(String(128))
    until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Recorded so the plane-type -> flight-speed factors in app.war.live_status can be
    # checked against real flights.
    plane_type: Mapped[str | None] = mapped_column(String(32))
    # Torn's Online / Idle / Offline label at this moment (for the target board's highlight).
    online_status: Mapped[str | None] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    player: Mapped[Player] = relationship(back_populates="status_history")


class ConfirmedStat(Base, TimestampMixin):
    """Manually entered, confirmed spy report (spec §24). Never mixed with estimates."""

    __tablename__ = "confirmed_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    # The server whose member filed it. Reports are that faction's private intel, so
    # every read filters on this; a NULL row (none exist — the column was added after
    # the table) is never shown anywhere. Nullable only so startup can ALTER it in.
    guild_id: Mapped[int | None] = mapped_column(BigInteger)

    strength: Mapped[float | None] = mapped_column(Float)
    defense: Mapped[float | None] = mapped_column(Float)
    speed: Mapped[float | None] = mapped_column(Float)
    dexterity: Mapped[float | None] = mapped_column(Float)
    total: Mapped[float | None] = mapped_column(Float)

    reported_by_discord_id: Mapped[int | None] = mapped_column(BigInteger)
    reported_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(256))

    player: Mapped[Player] = relationship(back_populates="confirmed_stats")


class TargetRanking(Base):
    """A computed target-priority snapshot for a war (spec §17)."""

    __tablename__ = "target_rankings"
    __table_args__ = (
        Index("ix_target_rankings_war_rank", "war_id", "rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)

    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)  # HIGH / MEDIUM / LOW
    algorithm_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    computed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )


class DiscordChannel(Base, TimestampMixin):
    """Tracks a live embed we edit in place rather than reposting (spec §31)."""

    __tablename__ = "discord_channels"
    __table_args__ = (
        UniqueConstraint("war_id", "purpose", name="uq_discord_channels_war_purpose"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    # information/stats/targets/log
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Primary live message for this channel; roster overflow messages are tracked
    # in ``extra_message_ids`` as a comma-separated list.
    live_message_id: Mapped[int | None] = mapped_column(BigInteger)
    extra_message_ids: Mapped[str | None] = mapped_column(String(512))

    war: Mapped[War] = relationship(back_populates="channel_messages")


class WarLogEvent(Base):
    """A meaningful war-log entry (spec §32). Not an API-request audit log."""

    __tablename__ = "war_log_events"
    __table_args__ = (
        Index("ix_war_log_events_war_time", "war_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    # Set once the event has been posted to the Discord war-log channel.
    posted_message_id: Mapped[int | None] = mapped_column(BigInteger)

    war: Mapped[War] = relationship(back_populates="log_events")


class WarHit(Base):
    """One attack from our faction's attack feed, stored during a war.

    Both directions are kept (``is_outgoing``): the newest ``started_at`` is the
    sync cursor, so it must advance past hits made *on* us too — otherwise a
    quiet spell on our side would re-fetch the same window every tick. Only
    outgoing hits are ever posted to the war log.

    This is a separate table (not extra columns on ``war_log_events``) because
    the schema is ``create_all``-managed: a new table appears on the live
    database automatically, whereas a new column on an existing table would not.
    """

    __tablename__ = "war_hits"
    __table_args__ = (
        UniqueConstraint("war_id", "torn_attack_id", name="uq_war_hits_war_attack"),
        Index("ix_war_hits_war_started", "war_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    war_id: Mapped[int] = mapped_column(ForeignKey("wars.id"), index=True)
    torn_attack_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    attacker_id: Mapped[int | None] = mapped_column(BigInteger)
    attacker_name: Mapped[str | None] = mapped_column(String(64))
    defender_id: Mapped[int | None] = mapped_column(BigInteger)
    defender_name: Mapped[str | None] = mapped_column(String(64))
    defender_faction_name: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    respect_gain: Mapped[float | None] = mapped_column(Float)

    # A war hit = Torn flags it as ranked-war OR it landed on the war enemy. Torn's
    # flag alone is not enough: it is only set on hits that *scored*, so failed,
    # interrupted and assist hits on the enemy come through unflagged.
    is_war_hit: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_outgoing: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Our chain's number at this hit (0 = not a chain hit): the payout sheet's "#Chain".
    chain: Mapped[int | None] = mapped_column(Integer)
    # Respect modifier from a Warlord-bonus weapon (1.0 = none, 1.15 = +15%). Torn's attack
    # feeds never name the weapon, but the Warlord bonus is carried by ranked-war weapons,
    # so > 1 is the payout sheet's "RW?": a ranked war weapon was used. (On live data,
    # 20% of hits, and only 6 of 37 players ever — the pattern of a weapon someone owns.)
    # New rows always have both of these; NULL only marks hits stored before the columns
    # existed, which ``WarHitService`` backfills.
    warlord: Mapped[float | None] = mapped_column(Float)
    # Set once the hit has been posted to the Discord war-log channel.
    posted_message_id: Mapped[int | None] = mapped_column(BigInteger)

    war: Mapped[War] = relationship(back_populates="hits")
