"""Async database engine and session management.

Exposes a single :class:`Database` object that owns the SQLAlchemy async engine
and hands out sessions via an ``async with db.session() as session:`` context
manager. The rest of the app depends on this object, never on a module-level
engine global, so tests can spin up an in-memory SQLite database in isolation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import Settings, get_settings
from app.database.models import Base
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database.

    aiosqlite will not create missing directories, so a fresh checkout with
    ``sqlite+aiosqlite:///./data/x.db`` would otherwise fail with
    "unable to open database file".
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        return
    if url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _add_missing_columns(conn: Connection) -> list[str]:
    """Add columns that the models have but an already-existing table lacks.

    ``create_all`` creates missing *tables* but never alters existing ones, and
    there are no Alembic migrations yet, so a new column on a live table would
    otherwise crash every query on it. Only nullable columns are added (a NOT NULL
    column cannot be added to a table with rows without a default); anything else
    is skipped and logged. Names come from our own models, never from user input.
    """
    inspector = inspect(conn)
    quote = conn.dialect.identifier_preparer.quote
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable:
                logger.warning(
                    "database.column_needs_migration", table=table.name, column=column.name
                )
                continue
            sql_type = column.type.compile(dialect=conn.dialect)
            conn.execute(
                text(
                    f"ALTER TABLE {quote(table.name)} "
                    f"ADD COLUMN {quote(column.name)} {sql_type}"
                )
            )
            added.append(f"{table.name}.{column.name}")
    return added


class Database:
    """Owns the async engine + session factory for one application instance."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if settings.is_sqlite:
            _ensure_sqlite_dir(settings.database_url)
        # SQLite needs no pool tuning; Postgres benefits from a modest pool with
        # pre-ping so a dropped connection is detected before a query fails.
        engine_kwargs: dict[str, object] = {"echo": False, "future": True}
        if not settings.is_sqlite:
            engine_kwargs.update(pool_size=5, max_overflow=5, pool_pre_ping=True)
        self._engine: AsyncEngine = create_async_engine(settings.database_url, **engine_kwargs)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def create_all(self) -> None:
        """Create tables from the ORM metadata.

        This is the MVP bootstrap path. Once Alembic migrations exist (see
        ``migrations/``), production should run ``alembic upgrade head`` instead
        and this becomes test-only.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            added = await conn.run_sync(_add_missing_columns)
        for column in added:
            logger.info("database.column_added", column=column)
        scheme = self._settings.database_url.split(":", 1)[0]
        logger.info("database.schema_ready", url_scheme=scheme)

    async def dispose(self) -> None:
        await self._engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    """Return the process-wide :class:`Database` singleton."""
    global _database
    if _database is None:
        _database = Database(get_settings())
    return _database
