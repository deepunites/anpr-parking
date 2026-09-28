"""§5: «Сетевой разрыв между API и БД на 30 с → без потерь, без зависших транзакций».
toxiproxy timeout=0 в обе стороны: пакеты «в чёрную дыру», RST нет — самый неприятный вариант для пулов."""

from __future__ import annotations

import asyncio
import time

import asyncpg
import httpx
from tests.conftest import _with_db
from tests.failure.lab import FailureLab
from tests.failure.support import db_event_ids, drain_offline, duplicate_sessions, send_batch, wait_until

from parking.runtime.container import Container


async def test_thirty_second_partition_no_loss_no_hung_transactions(
    lab: FailureLab, client: httpx.AsyncClient, container: Container, entry_key: str, test_dsn: str
) -> None:
    await container.redis.delete("anpr:offline")
    direct = _with_db(lab.direct_dsn(lab.primary_port), "parking_test")
    before = await send_batch(client, entry_key, 10)
    assert set(before.statuses()) == {200}
    lab.partition(enabled=True)
    t0 = time.monotonic()
    latencies: list[float] = []
    sent = None
    try:
        while time.monotonic() - t0 < 30:
            started = time.monotonic()
            sent = await send_batch(client, entry_key, 1, sent, prefix="05")
            latencies.append(time.monotonic() - started)
            await asyncio.sleep(0.5)
    finally:
        lab.partition(enabled=False)
    assert sent is not None
    statuses = sent.statuses()
    assert set(statuses) <= {200, 202}, f"во время разрыва только 2xx: {statuses}"
    fast = sum(1 for latency in latencies if latency < 1.0)
    assert fast >= len(latencies) // 2, f"после открытия breaker'а ответы из буфера быстрые: {fast}/{len(latencies)}"
    assert max(latencies) < 15, f"клиентский таймаут ограничивает ожидание: max {max(latencies):.1f}s"

    async def healthy() -> bool:
        resp = await send_batch(client, entry_key, 1, prefix="06")
        return resp.statuses() == [200] and not container.db_breaker.is_open

    recovered = await wait_until(healthy, limit=30)
    conn = await asyncpg.connect(direct)
    try:
        hung = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' AND xact_start < now() - interval '10 seconds'"
        )
        active_old = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND query_start < now() - interval '15 seconds' AND backend_type = 'client backend'"
        )
    finally:
        await conn.close()
    assert hung == 0 and active_old == 0, f"зависшие транзакции: idle-in-tx={hung}, active>15s={active_old}"
    processed, duplicates = await drain_offline(container)
    in_db = await db_event_ids(direct)
    assert (before.accepted | sent.accepted) <= in_db and duplicates == 0
    assert await duplicate_sessions(direct) == 0
    print(
        f"\nGATE partition 30s: {statuses.count(200)}×200 {statuses.count(202)}×202, fast(<1s) {fast}/{len(latencies)}, max latency {max(latencies):.1f}s, "
        f"recovered in {recovered:.1f}s, hung transactions 0, replayed {processed}, duplicates {duplicates}"
    )
