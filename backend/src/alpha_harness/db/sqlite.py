"""Async SQLAlchemy engine, session factory, and the additive schema migration.

WAL mode plus a busy timeout: the background simulation tracker writes while HTTP
handlers read, and SQLite's default rollback journal would make them block each other.

``create_all`` only creates missing *tables*, so a column added to a model after the
database was created never appears and surfaces much later as ``no such column``.
:func:`migrate` closes that gap on every startup by comparing the live schema against
``Base.metadata``.

**Additive, with two exceptions.** A column that has *disappeared* from a model is reported
rather than quietly reconciled, unless it is listed in :data:`RETIRED`, or left ``NOT NULL``
with no default: nothing writes that one, so it blocks every insert into its table, and it
is dropped because nothing reads it either.
"""

import re
import sqlite3
from contextlib import asynccontextmanager, closing
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import Index, Table, event, inspect, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateColumn

from .models import Base

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _apply_pragmas(dbapi_connection: Any, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, path: Path) -> None:
        self._engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        event.listen(self._engine.sync_engine, "connect", _apply_pragmas)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Bring the schema up to the models, dropping only what :func:`migrate` says.

        Not just ``create_all``: that leaves an existing table alone, so a column added to
        a model later never appears. See :func:`migrate`.
        """
        async with self._engine.begin() as conn:
            await migrate(conn)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        async with self._engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return result.scalar() == 1

    async def dispose(self) -> None:
        await self._engine.dispose()


#: Snapshots kept, one per version that started against this database.
SNAPSHOTS_KEPT = 3


def snapshot(path: Path, version: str) -> Path | None:
    """Copy the database aside the first time ``version`` starts, before it migrates.

    Migration can drop columns, and falling back to the previous build restores its code
    but not this file. ``VACUUM INTO`` rather than a file copy, which can tear a WAL
    database, and rather than the backup API, which copies free pages too: measured, 0.4 s
    and 226 MB against 4.5 s and 749 MB. Returns the new snapshot, or ``None`` when there
    was nothing to do.
    """
    if not path.exists():
        return None
    folder = path.parent / "backups"
    target = folder / f"{path.stem}-before-{re.sub(r'[^\w.+-]', '_', version)}.db"
    if target.exists():
        return None
    folder.mkdir(mode=0o700, exist_ok=True)
    partial = target.with_suffix(".partial")
    # VACUUM INTO refuses a file that is already there, as one a crash left half-written is.
    partial.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as source:
        source.execute("VACUUM INTO ?", [str(partial)])
    partial.replace(target)
    older = sorted(folder.glob(f"{path.stem}-before-*.db"), key=lambda p: p.stat().st_mtime)
    for stale in older[:-SNAPSHOTS_KEPT]:
        stale.unlink()
    log.info("db.snapshot", path=str(target))
    return target


# --- migration -------------------------------------------------------------------

log = structlog.get_logger(__name__)


class MigrationError(RuntimeError):
    """A schema change this migrator will not make on its own."""


#: Columns a model gave up on purpose, dropped wherever they are found. Only ones an older
#: build can put back, nullable or with a server default: the launcher falls back to the
#: previous build when a new one fails to start, and that build re-adds what its models declare.
RETIRED = {
    "sync_run": {"cursor_dataset", "fields_expected"},
    "brain_session": {"user_id", "expires_at"},
    "trial": {"constraint", "feasible"},
}


async def migrate(connection: AsyncConnection) -> None:
    """Create what is missing and log what was done."""
    live_tables = set(await connection.run_sync(_table_names))
    await connection.run_sync(Base.metadata.create_all)

    added_columns: list[str] = []
    added_indexes: list[str] = []
    unknown_columns: list[str] = []
    dropped_columns: list[str] = []
    retired_columns: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue  # create_all just made it, so it is current by construction.

        present = await _columns(connection, table.name)

        for column in table.columns:
            if column.name in present:
                continue
            ddl = _add_column_sql(table, column)
            await connection.execute(text(ddl))
            added_columns.append(f"{table.name}.{column.name}")

        expected = {c.name for c in table.columns}
        for name in sorted(present.keys() - expected):
            retired = name in RETIRED.get(table.name, ())
            # A leftover column is harmless unless it is NOT NULL with no default: nothing
            # writes it, so every insert into the table fails forever. Measured on a
            # consultant's machine, where `study.sampler_notes` from an older build made
            # every lab's Add Task answer 500.
            if (retired or present[name]) and await _drop_column(connection, table.name, name):
                (retired_columns if retired else dropped_columns).append(f"{table.name}.{name}")
            else:
                unknown_columns.append(f"{table.name}.{name}")

        added_indexes.extend(
            [index.name or "" for index in table.indexes if await _create_index(connection, index)]
        )

    if added_columns or added_indexes or retired_columns:
        log.info(
            "db.migrated", columns=added_columns, indexes=added_indexes, retired=retired_columns
        )
    if dropped_columns:
        log.warning(
            "db.dropped_blocking_columns",
            columns=dropped_columns,
            detail=(
                "Left by an older build, NOT NULL with no default, so nothing could be "
                "inserted into their tables. Nothing read them."
            ),
        )
    if unknown_columns:
        # Not fatal: a nullable extra column costs nothing at runtime, and dropping it is
        # the destructive act this migrator refuses. Still worth saying once per start.
        log.warning(
            "db.unknown_columns",
            columns=unknown_columns,
            detail=(
                "These exist in the database but not in the models. Nothing reads them. "
                "Listing one in RETIRED drops it."
            ),
        )


def _table_names(sync_connection: Any) -> list[str]:
    return list(inspect(sync_connection).get_table_names())


async def _columns(connection: AsyncConnection, table: str) -> dict[str, bool]:
    """Every live column, mapped to whether a row could be inserted without naming it.

    ``PRAGMA table_info`` answers ``(cid, name, type, notnull, dflt_value, pk)``. A primary
    key is exempt: SQLite fills an ``INTEGER PRIMARY KEY`` itself.
    """
    result = await connection.execute(text(f'PRAGMA table_info("{table}")'))
    return {row[1]: bool(row[3]) and row[4] is None and not row[5] for row in result.fetchall()}


async def _drop_column(connection: AsyncConnection, table: str, column: str) -> bool:
    """Drop a column, reporting whether SQLite allowed it.

    It refuses one an index or a generated column depends on, which is a refusal to report
    rather than a reason to stop starting up.
    """
    try:
        await connection.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
    except OperationalError as exc:
        log.warning("db.drop_column_refused", table=table, column=column, error=str(exc))
        return False
    return True


async def _create_index(connection: AsyncConnection, index: Index) -> bool:
    """Create an index if it is missing. Returns whether it was created."""
    existing = await connection.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = :name"),
        {"name": index.name},
    )
    if existing.first() is not None:
        return False
    await connection.run_sync(index.create)
    return True


def _add_column_sql(table: Table, column: Any) -> str:
    """``ALTER TABLE ... ADD COLUMN`` for one column.

    SQLite will not add a ``NOT NULL`` column to a table that already has rows without a
    constant default, so a new column is nullable or declares a ``server_default``;
    anything else gets a real migration.
    """
    if not column.nullable and column.server_default is None:
        raise MigrationError(
            f"Cannot add {table.name}.{column.name}: it is NOT NULL with no server_default, "
            "so existing rows have no value to take. Make it nullable, give it a "
            "server_default, or write a real migration."
        )
    rendered = CreateColumn(column).compile(dialect=sqlite.dialect())
    return f'ALTER TABLE "{table.name}" ADD COLUMN {rendered}'
