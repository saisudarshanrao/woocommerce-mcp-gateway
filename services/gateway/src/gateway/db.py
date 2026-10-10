"""The database connection pool and schema migrations."""

from __future__ import annotations

import asyncio
import json
import logging
from importlib import resources
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# Any fixed number: it only has to be the same in every gateway replica.
MIGRATION_LOCK_ID = 7_241_001


async def _prepare_connection(connection: asyncpg.Connection) -> None:
    # Hand jsonb columns to the application as Python objects.
    await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    """The connection pool, in a form that can exist before it is connected.

    asyncpg can only build a pool inside a running event loop, but the
    application wires its parts together before the loop starts. This object is
    handed around in the pool's place and passes every call on to the real pool
    once `connect()` has run, so the rest of the code just treats it as a pool.
    """

    def __init__(self, database_url: str):
        self._url = database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self, retry_for_seconds: float = 0) -> None:
        """Opens the pool, retrying for a while if the database is not there yet.

        At deploy time the database pod may still be starting, and a network
        policy that admits this pod may not have caught up with its address.
        Waiting here is quieter than crashing and being restarted.
        """
        deadline = asyncio.get_running_loop().time() + retry_for_seconds
        while True:
            try:
                self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=10, init=_prepare_connection)
                return
            except (OSError, asyncpg.PostgresError) as error:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                logger.info("waiting for the database: %s", error)
                await asyncio.sleep(2)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def __getattr__(self, name: str):
        # Reached only for names not defined above: fetch, fetchrow, execute, acquire...
        if self._pool is None:
            raise RuntimeError("the database is not connected yet")
        return getattr(self._pool, name)


def migration_files() -> list[Path]:
    packaged = resources.files("gateway") / "migrations"
    directory = Path(str(packaged))
    if not directory.is_dir():
        # Running from a source checkout, where migrations sit beside src/.
        directory = Path(__file__).resolve().parents[2] / "migrations"
    return sorted(directory.glob("*.sql"))


async def migrate(pool: asyncpg.Pool) -> list[str]:
    """Applies migrations that have not run yet, in file-name order.

    Each file runs once, inside a transaction, and is then recorded. An advisory
    lock makes replicas that start together take turns, so a migration is never
    applied twice.
    """
    applied_now: list[str] = []
    async with pool.acquire() as connection:
        await connection.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
        try:
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {row["name"] for row in await connection.fetch("SELECT name FROM schema_migrations")}
            for file in migration_files():
                if file.name in done:
                    continue
                async with connection.transaction():
                    await connection.execute(file.read_text())
                    await connection.execute("INSERT INTO schema_migrations (name) VALUES ($1)", file.name)
                applied_now.append(file.name)
                logger.info("applied migration %s", file.name)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)
    return applied_now
