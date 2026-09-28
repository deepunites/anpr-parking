"""§5: «PostgreSQL остановлен → API 202, события в Redis Stream, потерь нет»; «PostgreSQL поднят → все события
доехали, дублей нет, счётчики сошлись». Кластер лаборатории останавливается по-настоящему (pg_ctl stop)."""

from __future__ import annotations

import httpx
from tests.conftest import _with_db
from tests.failure.lab import FailureLab
from tests.failure.support import (
    buffered_event_ids,
    db_event_ids,
    drain_offline,
    duplicate_sessions,
    send_batch,
    wait_until,
)

from parking.runtime.container import Container


async def test_postgres_stopped_then_started_no_loss_no_duplicates(
    lab: FailureLab, client: httpx.AsyncClient, container: Container, entry_key: str, test_dsn: str
) -> None:
    dsn = _with_db(test_dsn, "parking_test")
    await container.redis.delete("anpr:offline")
    before = await send_batch(client, entry_key, 20)
    assert set(before.statuses()) == {200}
    lab.stop_primary()
    try:
        outage = await send_batch(client, entry_key, 40, prefix="02")
    finally:
        lab.start_primary()
    statuses = outage.statuses()
    assert set(statuses) <= {200, 202, 503} and statuses.count(202) >= 35, statuses
    assert 503 not in statuses, "буфер доступен — 503 недопустим"
    first_202 = statuses.index(202)
    assert statuses[:first_202].count(200) <= 5, "breaker открылся после первых же отказов"
    queued = outage.event_ids(202)
    assert queued <= await buffered_event_ids(container), "каждое 202 — в anpr:offline (I-4)"

    async def db_back() -> bool:
        return (await client.get("/metrics")).status_code == 200 and not container.db_breaker.is_open

    recovered_in = await wait_until(db_back, limit=30)
    processed, duplicates = await drain_offline(container)
    in_db = await db_event_ids(dsn)
    accepted = before.accepted | outage.accepted
    assert accepted <= in_db, f"потеряно {len(accepted - in_db)} событий с 2xx"
    assert processed >= len(queued) and duplicates == 0
    assert await duplicate_sessions(dsn) == 0
    assert await container.offline_buffer.length() == 0
    print(
        f"\nGATE pg outage: {len(before.accepted)}×200 до остановки; во время: {statuses.count(200)}×200 "
        f"{statuses.count(202)}×202; после старта replay processed={processed} duplicates={duplicates}; "
        f"всё принятое в БД ({len(accepted)}), дублей сессий 0; восстановление за {recovered_in:.1f}s"
    )
