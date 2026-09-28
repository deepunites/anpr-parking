"""I-3. Повторная доставка camera-события не создаёт вторую сессию и не открывает шлагбаум второй раз.

Механизм: PRIMARY KEY (event_second, idempotency_key) на anpr_idempotency; event_second = floor(event_time) входит в
вычисление ключа, поэтому одинаковые ключи всегда в одной партиции — уникальность глобальна при дневных партициях.
При нарушении: второй INSERT проходит, приложение создаёт вторую сессию и вторую команду шлагбауму.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import asyncpg
import pytest

from parking.platform.db.tenant import tenant_transaction
from tests.conftest import CAM_A1_IN, TENANT_A

SECOND = datetime.now(tz=UTC).replace(microsecond=0)


async def _insert(conn: asyncpg.Connection, key: bytes) -> None:
    await conn.execute(
        """
        INSERT INTO anpr_idempotency (idempotency_key, event_second, tenant_id, camera_id, result, response)
        VALUES ($1, $2, $3, $4, 'ENTRY_CONFIRMED', '{"status": "ENTRY_CONFIRMED"}')
        """,
        key,
        SECOND,
        TENANT_A,
        CAM_A1_IN,
    )


async def test_same_key_twice_is_rejected(critical: asyncpg.Connection) -> None:
    key = hashlib.sha256(b"I03-twice").digest()
    async with tenant_transaction(critical, TENANT_A):
        await _insert(critical, key)
    async with tenant_transaction(critical, TENANT_A):
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert(critical, key)


async def test_hundred_parallel_identical_events_one_winner(test_dsn: str) -> None:
    key = hashlib.sha256(b"I03-parallel").digest()
    pool = await asyncpg.create_pool(test_dsn.replace("postgres@", "app_critical@", 1), min_size=10, max_size=10)
    try:

        async def attempt() -> bool:
            async with pool.acquire() as conn:
                try:
                    async with tenant_transaction(conn, TENANT_A):
                        await _insert(conn, key)
                    return True
                except asyncpg.UniqueViolationError:
                    return False

        results = await asyncio.gather(*(attempt() for _ in range(100)))
    finally:
        await pool.close()
    assert sum(results) == 1


async def test_partition_pruning_on_lookup(owner: asyncpg.Connection) -> None:
    key = hashlib.sha256(b"I03-prune").digest()
    await _insert(owner, key)
    plan = "\n".join(
        r[0]
        for r in await owner.fetch(
            "EXPLAIN SELECT response FROM anpr_idempotency WHERE event_second = $1 AND idempotency_key = $2",
            SECOND,
            key,
        )
    )
    assert "Index Scan" in plan or "Index Only Scan" in plan
    assert plan.count(" on anpr_idempotency_") == 1, plan  # одна партиция
