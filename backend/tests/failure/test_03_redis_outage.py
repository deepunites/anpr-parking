"""§5: «Redis остановлен → деградация: кеш доступа мимо, оффлайн-буфер недоступен → API 503 честно»;
«Redis поднят → восстановление без ручных действий»."""

from __future__ import annotations

import asyncio

import httpx
from tests.failure.lab import FailureLab
from tests.failure.support import send_batch, wait_until
from tests.support.camera_fixtures import event_body, send_event, unique_plate

from parking.runtime.container import Container


async def test_redis_stopped_degrades_honestly_and_recovers(
    lab: FailureLab, client: httpx.AsyncClient, container: Container, entry_key: str
) -> None:
    ok = await send_batch(client, entry_key, 3)
    assert set(ok.statuses()) == {200}
    lab.redis("stop")
    try:
        await asyncio.sleep(0.5)
        # БД жива: событие обрабатывается — кэш ключей/решений мимо, rate-limit fail-open, ответ 200
        degraded = await send_batch(client, entry_key, 5, prefix="07")
        assert set(degraded.statuses()) == {200}, degraded.statuses()
        # …а если ещё и БД недоступна — честный 503, а не 2xx без записи (I-4)
        breaker = container.db_breaker
        for _ in range(breaker.threshold):
            breaker.record_failure()
        try:
            resp = await send_event(client, entry_key, event_body(unique_plate("08")))
            assert resp.status_code == 503, resp.text
        finally:
            breaker.record_success()
        metrics_resp = await client.get("/metrics")
        assert metrics_resp.status_code == 200 and 'dependency_up{dependency="redis"} 0.0' in metrics_resp.text
    finally:
        lab.redis("start")

    async def redis_back() -> bool:
        try:
            return bool(await container.redis.ping())
        except Exception:
            return False

    recovered = await wait_until(redis_back, limit=30)
    after = await send_batch(client, entry_key, 3, prefix="09")
    assert set(after.statuses()) == {200}
    await asyncio.sleep(5.5)  # MetricsRefresher: min_interval 5 с
    metrics_resp = await client.get("/metrics")
    assert 'dependency_up{dependency="redis"} 1.0' in metrics_resp.text
    print(
        f"\nGATE redis outage: degraded 5×200 (cache bypass), DB+Redis down → 503, recovered in {recovered:.1f}s without manual action"
    )
