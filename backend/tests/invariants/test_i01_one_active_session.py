"""I-1. В паре (lot, vehicle) не может быть больше одной сессии в статусе ACTIVE.

Механизм: partial unique index uq_session_one_active (lot_id, vehicle_id) WHERE status = 'ACTIVE' на HASH-партиционированной
parking_session (ключ партиционирования lot_id входит в индекс).
При нарушении: вторая транзакция ждёт первую и получает UniqueViolationError; приложение возвращает существующую сессию.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import asyncpg
import pytest

from parking.platform.db.tenant import tenant_transaction
from tests.conftest import CAM_A1_IN, LOT_A1, TENANT_A, new_session, new_vehicle


async def test_second_active_session_waits_then_fails(
    owner: asyncpg.Connection, critical: asyncpg.Connection, critical2: asyncpg.Connection
) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I01-RACE-1")

    tx1 = critical.transaction()
    await tx1.start()
    await critical.execute("SELECT set_config('app.current_tenant_id', $1, true)", str(TENANT_A))
    first = await new_session(critical, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)

    async def second_insert() -> UUID:
        async with tenant_transaction(critical2, TENANT_A):
            return await new_session(critical2, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)

    task = asyncio.create_task(second_insert())
    await asyncio.sleep(0.3)
    assert not task.done(), "вторая вставка обязана ждать коммита первой, а не проходить"
    await tx1.commit()
    with pytest.raises(asyncpg.UniqueViolationError) as exc:
        await task
    assert "uq_session_one_active" in str(exc.value) or "lot_id_vehicle_id" in str(exc.value)

    rows = await owner.fetch(
        "SELECT id FROM parking_session WHERE lot_id = $1 AND vehicle_id = $2 AND status = 'ACTIVE'", LOT_A1, vehicle_id
    )
    assert [UUID(str(r["id"])) for r in rows] == [first]


async def test_fifty_concurrent_entries_yield_one_session(owner: asyncpg.Connection, test_dsn: str) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I01-RACE-50")
    pool = await asyncpg.create_pool(test_dsn.replace("postgres@", "app_critical@", 1), min_size=10, max_size=10)
    try:

        async def attempt() -> bool:
            async with pool.acquire() as conn:
                try:
                    async with tenant_transaction(conn, TENANT_A):
                        await new_session(conn, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)
                    return True
                except asyncpg.UniqueViolationError:
                    return False

        results = await asyncio.gather(*(attempt() for _ in range(50)))
    finally:
        await pool.close()
    assert sum(results) == 1
    count = await owner.fetchval(
        "SELECT count(*) FROM parking_session WHERE lot_id = $1 AND vehicle_id = $2 AND status = 'ACTIVE'",
        LOT_A1,
        vehicle_id,
    )
    assert count == 1


async def test_closed_sessions_are_unlimited(owner: asyncpg.Connection) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I01-CLOSED")
    for _ in range(3):
        await owner.execute(
            """
            INSERT INTO parking_session (tenant_id, lot_id, vehicle_id, entry_camera_id, entry_time, exit_time,
                                         billing_minutes, status, exit_kind)
            VALUES ($1, $2, $3, $4, now() - interval '2 hours', now(), 120, 'CLOSED', 'CAMERA')
            """,
            TENANT_A,
            LOT_A1,
            vehicle_id,
            CAM_A1_IN,
        )
    assert await owner.fetchval("SELECT count(*) FROM parking_session WHERE vehicle_id = $1", vehicle_id) == 3
