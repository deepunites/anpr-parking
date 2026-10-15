"""Обслуживание площадок по расписанию (celery «sweep-stale-sessions», раз в 5 минут): сессии старше
max_session_hours → ORPHAN и счётчик занятости каждой площадки = точное число машин в базе. До 2026-10-15 задача без
tenant_id ничего не делала, а счётчик, разошедшийся с базой (массовая чистка, сбой Redis), оставался неверным
навсегда — «Живая парковка» показывала 0 из 54 при 20 машинах."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg

from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1
from tests.support.parking_fixtures import entry_cmd


def _plate() -> PlateNumber:
    n = uuid4().int
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"
    return PlateNumber.parse(
        f"{10 + n % 90}{letters[n // 90 % 23]}{100 + n // 2070 % 900}{letters[n // 1863000 % 23]}{letters[n // 42849000 % 23]}"
    )


async def test_maintenance_sweeps_old_sessions_and_fixes_occupancy(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    now = datetime.now(tz=UTC)
    old = await container.parking.process_entry(entry_cmd(_plate(), at=now - timedelta(days=4)))
    parked = [await container.parking.process_entry(entry_cmd(_plate(), at=now - timedelta(minutes=m))) for m in (5, 3)]
    await container.occupancy.set(LOT_A1, -40)  # разошёлся: на экране «0 из 54»

    closed = await container.parking.maintain_lots(container.saga_scanner)

    assert closed >= 1
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", old.session_id) == "ORPHAN"
    exact = await owner.fetchval("SELECT count(*) FROM parking_session WHERE lot_id = $1 AND status = 'ACTIVE'", LOT_A1)
    assert exact == len(parked) and await container.occupancy.get(LOT_A1) == exact
