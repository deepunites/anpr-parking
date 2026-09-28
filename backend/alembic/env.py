"""Alembic: async-движок asyncpg, миграции — чистый SQL из alembic/sql/*.sql.

DSN берётся из PARKING_DATABASE_URL; без него — локальный pgserver (backend/.pgdata, БД parking_dev).
"""

from __future__ import annotations

import asyncio
import logging.config
import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # sqlfiles.py рядом с env.py

config = context.config
if config.config_file_name is not None:
    logging.config.fileConfig(config.config_file_name)


def database_url() -> str:
    url = os.environ.get("PARKING_DATABASE_URL")
    if url:
        return url
    pgdata = HERE.parent / ".pgdata"
    return f"postgresql+asyncpg://postgres@/parking_dev?host={pgdata}"


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config({"sqlalchemy.url": database_url()}, prefix="sqlalchemy.", poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
