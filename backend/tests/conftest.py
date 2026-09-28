"""Тестовая БД: реальный PostgreSQL 16.

Приоритет: PARKING_TEST_DSN (внешний кластер, например testcontainers в CI) → локальный pgserver (backend/.pgdata),
который поднимается при необходимости. Никаких sqlite-подмен и мокнутых репозиториев (фаза 8 §3).
На каждую сессию pytest: DROP/CREATE DATABASE parking_test, alembic upgrade head, сид scripts/seed_dev.sql.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import asyncpg
import pytest

pytest_plugins = [
    "tests.support.api_fixtures",
    "tests.support.parking_fixtures",
    "tests.support.camera_fixtures",
    "tests.support.billing_fixtures",
]

BACKEND = Path(__file__).resolve().parent.parent
PGDATA = BACKEND / ".pgdata"
TEST_DB = "parking_test"

TENANT_A = UUID("00000000-0000-4000-8000-0000000000a1")
TENANT_B = UUID("00000000-0000-4000-8000-0000000000b1")
LOT_A1 = UUID("00000000-0000-4000-8000-00000000a101")
LOT_A2 = UUID("00000000-0000-4000-8000-00000000a102")
LOT_B1 = UUID("00000000-0000-4000-8000-00000000b101")
CAM_A1_IN = UUID("00000000-0000-4000-8000-00000000c101")
CAM_B1_IN = UUID("00000000-0000-4000-8000-00000000c105")
CAM_A1_OUT = UUID("00000000-0000-4000-8000-00000000c102")
BARRIER_A1_IN = UUID("00000000-0000-4000-8000-00000000e101")
BARRIER_A1_OUT = UUID("00000000-0000-4000-8000-00000000e102")
KEY_A1_IN = UUID("00000000-0000-4000-8000-0000000d0101")
USER_A_ADMIN = UUID("00000000-0000-4000-8000-0000000a0001")
USER_A_CASHIER = UUID("00000000-0000-4000-8000-0000000a0003")
USER_B_ADMIN = UUID("00000000-0000-4000-8000-0000000b0001")
SHIFT_A = UUID("00000000-0000-4000-8000-000000a50001")
TPV_A1 = UUID("00000000-0000-4000-8000-0000000a1101")
PLAN_A1 = UUID("00000000-0000-4000-8000-0000000a1001")


def _admin_dsn() -> str:
    external = os.environ.get("PARKING_TEST_DSN")
    if external:
        return external
    subprocess.run([sys.executable, str(BACKEND / "scripts" / "pg_dev.py"), "start"], check=True, capture_output=True)
    return f"postgresql://postgres@/postgres?host={PGDATA}"


def _with_db(dsn: str, dbname: str, user: str | None = None) -> str:
    """Меняет БД (и при необходимости пользователя) в DSN, не трогая query-параметры (host=/socket/dir)."""
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user:
        hostpart = netloc.rpartition("@")[2]
        netloc = f"{user}@{hostpart}"
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, parts.fragment))


@pytest.fixture(scope="session")
def test_dsn() -> str:
    admin = _admin_dsn()
    import asyncio

    async def prepare() -> None:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            await conn.execute(f"CREATE DATABASE {TEST_DB}")
        finally:
            await conn.close()

    asyncio.run(prepare())
    url = _with_db(admin, TEST_DB)
    env = {**os.environ, "PARKING_DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    async def seed() -> None:
        conn = await asyncpg.connect(url)
        try:
            await conn.execute((BACKEND / "scripts" / "seed_dev.sql").read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(seed())
    return url


@pytest.fixture
async def owner(test_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(test_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _role_conn(test_dsn: str, role: str) -> asyncpg.Connection:
    return await asyncpg.connect(_with_db(test_dsn, TEST_DB, user=role))


@pytest.fixture
async def critical(test_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await _role_conn(test_dsn, "app_critical")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def critical2(test_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await _role_conn(test_dsn, "app_critical")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def bulk(test_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await _role_conn(test_dsn, "app_bulk")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def maintenance(test_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await _role_conn(test_dsn, "app_maintenance")
    try:
        yield conn
    finally:
        await conn.close()


async def expect_error(
    conn: asyncpg.Connection, tenant_id: UUID, exc: type[Exception], stmt: str, *args: object, match: str | None = None
) -> None:
    """Каждая ожидаемая ошибка — в собственной транзакции: после ошибки PostgreSQL отвергает всё до ROLLBACK."""
    from parking.platform.db.tenant import tenant_transaction

    with pytest.raises(exc, match=match):
        async with tenant_transaction(conn, tenant_id):
            await conn.execute(stmt, *args)


def plate_hash(plate: str) -> bytes:
    """Заглушка HMAC для тестов уровня БД (реальный HMAC — фаза 3.1)."""
    import hashlib

    return hashlib.sha256(plate.encode()).digest()


async def new_vehicle(conn: asyncpg.Connection, tenant_id: UUID, plate: str) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO vehicle (tenant_id, plate_hash, plate_encrypted)
        VALUES ($1, $2, $3) ON CONFLICT (tenant_id, plate_hash) DO UPDATE SET last_seen_at = now() RETURNING id
        """,
        tenant_id,
        plate_hash(plate),
        plate_hash(plate) + plate_hash("enc"),
    )
    assert row is not None
    return UUID(str(row["id"]))


async def new_session(
    conn: asyncpg.Connection, tenant_id: UUID, lot_id: UUID, vehicle_id: UUID, camera_id: UUID, status: str = "ACTIVE"
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO parking_session (tenant_id, lot_id, vehicle_id, entry_camera_id, entry_time, status)
        VALUES ($1, $2, $3, $4, now(), $5::session_status) RETURNING id
        """,
        tenant_id,
        lot_id,
        vehicle_id,
        camera_id,
        status,
    )
    assert row is not None
    return UUID(str(row["id"]))
