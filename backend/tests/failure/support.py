"""Общие помощники failure-тестов: отправка пачек событий камер, сверка «отправлено == в БД + в буфере»."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
from tests.support.camera_fixtures import event_body, send_event, unique_plate

from parking.runtime.container import Container


@dataclass
class Sent:
    """Что мы отправили и что ответил API — источник истины для сверки потерь."""

    responses: list[tuple[str, int, dict[str, Any] | None]] = field(default_factory=list)

    def statuses(self) -> list[int]:
        return [status for _, status, _ in self.responses]

    def event_ids(self, *statuses: int) -> set[str]:
        return {body["event_id"] for _, status, body in self.responses if body and (not statuses or status in statuses)}

    @property
    def accepted(self) -> set[str]:
        return self.event_ids(200, 202)


async def send_batch(
    client: httpx.AsyncClient, key: str, count: int, sent: Sent | None = None, *, prefix: str = "01"
) -> Sent:
    sent = sent or Sent()
    for _ in range(count):
        plate = unique_plate(prefix)
        try:
            resp = await send_event(client, key, event_body(plate))
        except (httpx.HTTPError, OSError) as exc:  # транспортная ошибка — тоже «ответ»
            sent.responses.append((plate, 0, {"event_id": "", "error": type(exc).__name__}))
            continue
        try:
            body = resp.json()
        except ValueError:
            body = None
        sent.responses.append((plate, resp.status_code, body))
    return sent


async def db_event_ids(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        return {str(r["id"]) for r in await conn.fetch("SELECT id FROM anpr_event")}
    finally:
        await conn.close()


async def duplicate_sessions(dsn: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM (SELECT lot_id, vehicle_id FROM parking_session WHERE status = 'ACTIVE' GROUP BY 1, 2 HAVING count(*) > 1) d"
            )
        )
    finally:
        await conn.close()


async def buffered_event_ids(container: Container) -> set[str]:
    entries = await container.redis.xrange("anpr:offline")
    return {f[b"event_id"].decode() for _, f in entries}


async def drain_offline(container: Container, *, attempts: int = 80) -> tuple[int, int]:
    from tests.support.fakes import no_sleep

    replayer = container.offline_replayer(consumer_name="failure-replayer", sleep=no_sleep, claim_idle_ms=0)
    processed = duplicates = 0
    for _ in range(attempts):
        report = await replayer.poll_once()
        processed += report.processed
        duplicates += report.duplicates
        if await container.offline_buffer.pending() == 0 and await container.offline_buffer.length() == 0:
            break
        await asyncio.sleep(0.2)
    return processed, duplicates


async def wait_until(predicate: Any, *, limit: float = 60.0, interval: float = 0.25) -> float:
    """Ждёт истинности async-предиката; возвращает затраченные секунды (RTO)."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    while loop.time() - started < limit:
        if await predicate():
            return loop.time() - started
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {limit}s")
