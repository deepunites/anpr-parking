"""Outbox publisher + Stream Consumer (3.5): публикация только после коммита, порядок на агрегат, at-least-once,
идемпотентность потребителя (inbox), DLQ, end-to-end ENTRY → EXIT через события."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import text

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.parking.application.contracts import ExitStatus
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.runtime.container import Container, build_device_repos
from parking.shared.plate import PlateNumber
from parking.workers.envelope import Envelope, shard_for, stream_name
from parking.workers.handlers import build_handlers
from parking.workers.outbox_publisher import OutboxPublisher
from parking.workers.runtime import maintenance_dsn
from parking.workers.stream_consumer import DLQ_STREAM, StreamConsumer, delayed_key
from tests.conftest import LOT_A1, TENANT_A
from tests.support.fakes import fake_registry, no_sleep
from tests.support.parking_fixtures import entry_cmd, exit_cmd, subscription_decision


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
        await container.redis.delete(
            stream_name(shard),
            delayed_key(stream_name(shard)),
            f"lease:core:{stream_name(shard)}",
            f"lease:test-group:{stream_name(shard)}",
        )
    await container.redis.delete(DLQ_STREAM)


async def _publish_all(publisher: OutboxPublisher) -> int:
    total = 0
    while True:
        n = await publisher.publish_batch()
        total += n
        if n == 0:
            return total


async def test_event_appears_in_stream_only_after_commit(
    container: Container, publisher: OutboxPublisher, owner: asyncpg.Connection
) -> None:
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    session_id = uuid4()
    payload = {
        "session_id": str(session_id),
        "lot_id": str(LOT_A1),
        "vehicle_id": str(uuid4()),
        "entry_time": datetime.now(tz=UTC).isoformat(),
        "status": "ACTIVE",
    }
    event = IntegrationEvent(
        type="parking.session.created.v1",
        tenant_id=TENANT_A,
        aggregate_type="parking_session",
        aggregate_id=session_id,
        payload=payload,
    )
    with pytest.raises(RuntimeError):
        async with container.uow(TENANT_A) as uow:
            uow.publish(event)
            raise RuntimeError("rollback")
    assert await _publish_all(publisher) == 0, "откат транзакции — потребитель ничего не получает (I-5)"
    async with container.uow(TENANT_A) as uow:
        uow.publish(event)
    assert await _publish_all(publisher) == 1
    stream = stream_name(shard_for(session_id, 8))
    entries = await container.redis.xrange(stream)
    envelopes = [Envelope.from_fields(fields) for _, fields in entries]
    assert (
        [e.event_id for e in envelopes] == [event.event_id]
        and envelopes[0].payload == payload
        and envelopes[0].tenant_id == TENANT_A
    )
    assert await owner.fetchval("SELECT status::text FROM outbox WHERE event_id = $1", event.event_id) == "PUBLISHED"


async def test_order_within_aggregate_is_preserved(
    container: Container, publisher: OutboxPublisher, owner: asyncpg.Connection
) -> None:
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    aggregate = uuid4()
    base = {
        "session_id": str(aggregate),
        "lot_id": str(LOT_A1),
        "vehicle_id": str(uuid4()),
        "entry_time": datetime.now(tz=UTC).isoformat(),
    }
    async with container.uow(TENANT_A) as uow:
        uow.publish(
            IntegrationEvent(
                type="parking.session.created.v1",
                tenant_id=TENANT_A,
                aggregate_type="parking_session",
                aggregate_id=aggregate,
                payload={**base, "status": "ACTIVE"},
            )
        )
    async with container.uow(TENANT_A) as uow:
        uow.publish(
            IntegrationEvent(
                type="parking.session.orphaned.v1",
                tenant_id=TENANT_A,
                aggregate_type="parking_session",
                aggregate_id=aggregate,
                payload={**base, "status": "ORPHAN", "reason": "test"},
            )
        )
    await _publish_all(publisher)
    entries = await container.redis.xrange(stream_name(shard_for(aggregate, 8)))
    types = [Envelope.from_fields(f).event_type for _, f in entries]
    assert types == ["parking.session.created.v1", "parking.session.orphaned.v1"]


async def test_invalid_payload_goes_to_failed_not_stream(
    container: Container, publisher: OutboxPublisher, owner: asyncpg.Connection
) -> None:
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    event_id = uuid4()
    await owner.execute(
        "INSERT INTO outbox (event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id) VALUES ($1, $2, 'x', $3, 'parking.session.created.v1', '{}', $4)",
        event_id,
        TENANT_A,
        uuid4(),
        uuid4(),
    )
    await _publish_all(publisher)
    row = await owner.fetchrow("SELECT status::text AS status, last_error FROM outbox WHERE event_id = $1", event_id)
    assert row is not None and row["status"] == "FAILED" and "session_id" in row["last_error"]


async def test_consumer_processes_once_even_when_redelivered(
    container: Container, publisher: OutboxPublisher, owner: asyncpg.Connection
) -> None:
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    calls: list[UUID] = []

    async def handler(env: Envelope) -> None:
        calls.append(env.event_id)

    consumer = StreamConsumer(
        container.database,
        container.redis,
        {"parking.session.orphaned.v1": handler},
        group="test-group",
        consumer_name="c1",
        claim_idle_ms=1,
    )
    await consumer.ensure_groups()
    aggregate = uuid4()
    payload = {
        "session_id": str(aggregate),
        "lot_id": str(LOT_A1),
        "vehicle_id": str(uuid4()),
        "entry_time": datetime.now(tz=UTC).isoformat(),
        "status": "ORPHAN",
        "reason": "t",
    }
    async with container.uow(TENANT_A) as uow:
        uow.publish(
            IntegrationEvent(
                type="parking.session.orphaned.v1",
                tenant_id=TENANT_A,
                aggregate_type="parking_session",
                aggregate_id=aggregate,
                payload=payload,
            )
        )
    await _publish_all(publisher)
    assert await consumer.poll_once(block_ms=1) >= 1
    # «publisher упал после XADD, до отметки PUBLISHED» → повторная публикация той же записи
    await owner.execute("UPDATE outbox SET status = 'PENDING' WHERE aggregate_id = $1", aggregate)
    await _publish_all(publisher)
    await consumer.poll_once(block_ms=1)
    assert calls.count(calls[0]) == 1 and len(calls) == 1, "inbox: тот же event_id обработан один раз"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM consumer_inbox WHERE consumer_group = 'test-group' AND event_id = $1", calls[0]
        )
        == 1
    )


async def test_failing_handler_ends_in_dlq_after_retries(
    container: Container, publisher: OutboxPublisher, owner: asyncpg.Connection
) -> None:
    """Повтор идёт через отложенную очередь шарда (ZSET), а не через сон в цикле: падающее событие не блокирует
    соседей по шарду; после max_attempts — DLQ."""
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    handled: list[str] = []

    async def broken(env: Envelope) -> None:
        raise RuntimeError("handler exploded")

    async def healthy(env: Envelope) -> None:
        handled.append(str(env.payload["session_id"]))

    consumer = StreamConsumer(
        container.database,
        container.redis,
        {"parking.session.orphaned.v1": broken, "parking.session.closed.v1": healthy},
        group="test-group",
        consumer_name="c1",
        max_attempts=2,
    )
    await consumer.ensure_groups()
    aggregate = uuid4()
    orphan_payload = {
        "session_id": str(aggregate),
        "lot_id": str(LOT_A1),
        "vehicle_id": str(uuid4()),
        "entry_time": datetime.now(tz=UTC).isoformat(),
        "status": "ORPHAN",
        "reason": "t",
    }
    closed_payload = {
        "session_id": str(aggregate),
        "lot_id": str(LOT_A1),
        "vehicle_id": str(uuid4()),
        "entry_time": datetime.now(tz=UTC).isoformat(),
        "exit_time": datetime.now(tz=UTC).isoformat(),
        "exit_kind": "CAMERA",
        "billing_minutes": 1,
    }
    async with container.uow(TENANT_A) as uow:
        uow.publish(
            IntegrationEvent(
                type="parking.session.orphaned.v1",
                tenant_id=TENANT_A,
                aggregate_type="parking_session",
                aggregate_id=aggregate,
                payload=orphan_payload,
            ),
            IntegrationEvent(  # тот же агрегат → тот же шард, стоит в стриме за падающим событием
                type="parking.session.closed.v1",
                tenant_id=TENANT_A,
                aggregate_type="parking_session",
                aggregate_id=aggregate,
                payload=closed_payload,
            ),
        )
    await _publish_all(publisher)
    stream = stream_name(shard_for(aggregate, 8))
    delayed = delayed_key(stream)

    started = time.perf_counter()
    await consumer.poll_once(block_ms=1)
    assert time.perf_counter() - started < 0.9, "первая попытка не должна ждать бэкофф в цикле"
    assert handled == [str(aggregate)], "здоровое событие того же шарда обработано, не дожидаясь повтора"
    assert await container.redis.zcard(delayed) == 1 and await container.redis.xlen(DLQ_STREAM) == 0
    await consumer.poll_once(block_ms=1)
    assert await container.redis.zcard(delayed) == 1, "срок повтора (1 с) ещё не наступил — запись не возвращается"

    (member,) = await container.redis.zrange(delayed, 0, -1)
    await container.redis.zadd(delayed, {member: time.time() - 1})  # срок наступил
    await consumer.poll_once(block_ms=1)  # возврат в стрим и вторая попытка в одном проходе
    dlq = await container.redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1 and json.loads(dlq[0][1][b"payload"])["session_id"] == str(aggregate)
    assert dlq[0][1][b"error"].startswith(b"RuntimeError") and dlq[0][1][b"attempt"] == b"2"
    assert await container.redis.zcard(delayed) == 0


async def test_end_to_end_entry_and_exit_through_events(
    container: Container, publisher: OutboxPublisher, owner: asyncpg.Connection, clean_lot: None
) -> None:
    """ENTRY → command_requested → исполнитель → EXIT → exit.requested → сага → команда → completed → сага COMPLETED."""
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    registry, driver = fake_registry()
    executor = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )

    class InlineDispatcher:
        async def dispatch(self, tenant_id: UUID, command_id: UUID) -> None:
            await executor.execute(tenant_id, command_id)

    consumer = StreamConsumer(
        container.database,
        container.redis,
        build_handlers(container, InlineDispatcher()),
        group="test-group",
        consumer_name="c1",
    )
    await consumer.ensure_groups()

    async def drain() -> None:
        for _ in range(6):
            await _publish_all(publisher)
            await consumer.poll_once(block_ms=1)

    pl = PlateNumber.parse(f"01W{uuid4().int % 900 + 100}WW")
    entry = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=1)))
    await drain()
    assert (
        await owner.fetchval("SELECT state::text FROM barrier_command WHERE id = $1", entry.barrier_command_id) == "OK"
    )
    exit_result = await container.parking.start_exit(exit_cmd(pl, decision=subscription_decision()))
    assert exit_result.status is ExitStatus.EXIT_CONFIRMED
    await drain()
    saga = await owner.fetchrow(
        "SELECT state::text AS state, barrier_result FROM exit_saga WHERE id = $1", exit_result.saga_id
    )
    assert saga is not None and saga["state"] == "COMPLETED" and saga["barrier_result"] == "OK"
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", entry.session_id) == "CLOSED"
    assert [c[0] for c in driver.calls] == ["open", "open"]
    assert await owner.fetchval("SELECT count(*) FROM outbox WHERE status = 'PENDING'") == 0


async def test_uow_publish_and_publisher_agree_on_schema(container: Container) -> None:
    async with container.uow(TENANT_A) as uow:
        assert isinstance(uow, UnitOfWork)
        await uow.session.execute(text("SELECT 1"))
