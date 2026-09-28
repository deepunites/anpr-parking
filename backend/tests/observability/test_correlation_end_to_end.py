"""Фаза 7 §1: одно событие камеры → один correlation_id во всех логах API → outbox publisher → stream consumer →
исполнитель шлагбаума. Проверяется на реальной цепочке (HTTP → PostgreSQL outbox → Redis Stream → обработчики)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.runtime.container import Container, build_device_repos
from parking.workers.envelope import stream_name
from parking.workers.handlers import build_handlers
from parking.workers.outbox_publisher import OutboxPublisher
from parking.workers.runtime import maintenance_dsn
from parking.workers.stream_consumer import DLQ_STREAM, StreamConsumer
from tests.observability.logcapture import capture_logs
from tests.support.camera_fixtures import event_body, unique_plate
from tests.support.fakes import fake_registry, no_sleep

REQUIRED_FIELDS = ("timestamp", "level", "service", "correlation_id", "message")


@pytest.fixture
async def publisher(container: Container) -> OutboxPublisher:
    pub = OutboxPublisher(
        container.database, container.redis, maintenance_dsn=maintenance_dsn(container.settings), batch_size=200
    )
    yield pub  # type: ignore[misc]
    await pub.release()


@pytest.fixture(autouse=True)
async def _clean_streams(container: Container) -> None:
    for shard in range(8):
        await container.redis.delete(stream_name(shard), f"lease:test-group:{stream_name(shard)}")
    await container.redis.delete(DLQ_STREAM)


def _consumer(container: Container) -> StreamConsumer:
    registry, _driver = fake_registry()
    executor = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )

    class InlineDispatcher:
        async def dispatch(self, tenant_id: UUID, command_id: UUID) -> None:
            await executor.execute(tenant_id, command_id)

    return StreamConsumer(
        container.database,
        container.redis,
        build_handlers(container, InlineDispatcher()),
        group="test-group",
        consumer_name="c1",
    )


async def test_one_camera_event_one_correlation_id(
    client: httpx.AsyncClient,
    container: Container,
    publisher: OutboxPublisher,
    entry_key: str,
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    consumer = _consumer(container)
    await consumer.ensure_groups()
    cid = str(uuid4())
    with capture_logs() as entries:
        resp = await client.post(
            "/api/v1/camera/events",
            json=event_body(unique_plate()),
            headers={"X-Camera-Key": entry_key, "X-Correlation-Id": cid},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-correlation-id"] == cid and resp.json()["correlation_id"] == cid
        for _ in range(4):  # publisher → consumer → (command_requested → исполнитель → completed)
            await publisher.publish_batch()
            await consumer.poll_once(block_ms=1)
    assert (
        await owner.fetchval(
            "SELECT state::text FROM barrier_command WHERE id = $1", UUID(resp.json()["barrier_command_id"])
        )
        == "OK"
    )
    # correlation_id хранится в outbox и в конверте события — потребитель восстанавливает его из конверта
    assert await owner.fetchval("SELECT count(*) FROM outbox WHERE correlation_id = $1", cid) >= 2

    events = {e["event"] for e in entries}
    stages = {
        "api": "anpr_event_accepted",
        "api.http": "http.request",
        "publisher": "outbox_event_published",
        "consumer": "consumer_handled",
        "executor": "barrier_command_completed",
    }
    missing = {stage: name for stage, name in stages.items() if name not in events}
    assert not missing, f"нет записей этапов: {missing}; видны: {sorted(events)}"
    for name in stages.values():
        for entry in (e for e in entries if e["event"] == name):
            assert entry.get("correlation_id") == cid, f"{name}: correlation_id={entry.get('correlation_id')!r}"
    # ни одна запись любого этапа не несёт другого correlation_id (кроме служебных без контекста)
    foreign = [e for e in entries if e.get("correlation_id") not in (None, cid)]
    assert not foreign, foreign
    accepted = next(e for e in entries if e["event"] == "anpr_event_accepted")
    assert (
        accepted["level"] == "info"
        and accepted.get("tenant_id")
        and accepted.get("camera_id")
        and accepted.get("lot_id")
    )
    print("\n".join("  → " + _render(e) for e in entries if e["event"] in stages.values()))


def _render(entry: dict[str, Any]) -> str:
    keep = {
        k: v
        for k, v in entry.items()
        if k
        in {
            *REQUIRED_FIELDS,
            "event",
            "event_type",
            "lot_id",
            "session_id",
            "camera_id",
            "result",
            "stream",
            "trace_id",
        }
    }
    return json.dumps(keep, default=str, ensure_ascii=False)
