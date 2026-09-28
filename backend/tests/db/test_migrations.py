"""Миграции применяются с нуля и откатываются до base (gate фазы 2)."""

from __future__ import annotations

import os
import subprocess
import sys

import asyncpg

from tests.conftest import BACKEND

HEAD = sorted(p.name[:4] for p in (BACKEND / "alembic" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))[-1]
PREVIOUS = f"{int(HEAD) - 1:04d}"


def _alembic(dsn: str, *args: str) -> str:
    env = {**os.environ, "PARKING_DATABASE_URL": dsn.replace("postgresql://", "postgresql+asyncpg://", 1)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


async def test_downgrade_base_then_upgrade_head_is_clean(test_dsn: str) -> None:
    conn = await asyncpg.connect(test_dsn)
    try:  # схема до 0011 не хранит обязательства без способа оплаты: тестовая БД очищается от платежей перед откатом
        await conn.execute("TRUNCATE payment CASCADE")
    finally:
        await conn.close()
    _alembic(test_dsn, "downgrade", "base")
    conn = await asyncpg.connect(test_dsn)
    try:
        leftover = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname IN ('public', 'app') AND tablename <> 'alembic_version'"
        )
        assert leftover == []
        types_left = await conn.fetchval(
            "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public' AND t.typtype = 'e'"
        )
        assert types_left == 0
    finally:
        await conn.close()
    out = _alembic(test_dsn, "upgrade", "head")
    assert f"Running upgrade {PREVIOUS} -> {HEAD}" in out
    assert f"{HEAD} (head)" in _alembic(test_dsn, "current")
    conn = await asyncpg.connect(test_dsn)
    try:
        await conn.execute((BACKEND / "scripts" / "seed_dev.sql").read_text(encoding="utf-8"))
    finally:
        await conn.close()


async def test_downgrade_one_and_back(test_dsn: str) -> None:
    _alembic(test_dsn, "downgrade", "-1")
    assert PREVIOUS in _alembic(test_dsn, "current")
    _alembic(test_dsn, "upgrade", "head")
    assert f"{HEAD} (head)" in _alembic(test_dsn, "current")
