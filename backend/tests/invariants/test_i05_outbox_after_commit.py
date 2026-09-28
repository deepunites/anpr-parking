"""I-5. Ни один integration event не публикуется раньше коммита транзакции, его породившей.

Механизм: события копятся в UnitOfWork и вставляются в таблицу outbox в той же транзакции; в Redis Streams их
переносит только OutboxPublisher из строк со статусом PENDING (после коммита). Прямого XADD из домена нет.
При нарушении: потребитель видит «сессия создана», а транзакция откатилась — сага на несуществующую сессию.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from parking.platform.events import IntegrationEvent
from parking.runtime.container import Container
from parking.workers.envelope import shard_for, stream_name
from parking.workers.outbox_publisher import OutboxPublisher
from parking.workers.runtime import maintenance_dsn
from tests.conftest import LOT_A1, TENANT_A


def _event(aggregate: object) -> IntegrationEvent:
    return IntegrationEvent(
        type="parking.session.created.v1",
        tenant_id=TENANT_A,
        aggregate_type="parking_session",
        aggregate_id=aggregate,  # type: ignore[arg-type]
        payload={
            "session_id": str(aggregate),
            "lot_id": str(LOT_A1),
            "vehicle_id": str(uuid4()),
            "entry_time": datetime.now(tz=UTC).isoformat(),
            "status": "ACTIVE",
        },
    )


async def test_rolled_back_transaction_publishes_nothing(container: Container, owner: asyncpg.Connection) -> None:
    aggregate = uuid4()
    stream = stream_name(shard_for(aggregate, 8))
    before = await container.redis.xlen(stream)
    with pytest.raises(RuntimeError):
        async with container.uow(TENANT_A) as uow:
            uow.publish(_event(aggregate))
            raise RuntimeError("rollback")
    assert await owner.fetchval("SELECT count(*) FROM outbox WHERE aggregate_id = $1", aggregate) == 0
    assert await container.redis.xlen(stream) == before


async def test_event_reaches_stream_only_via_outbox_after_commit(
    container: Container, owner: asyncpg.Connection
) -> None:
    aggregate = uuid4()
    stream = stream_name(shard_for(aggregate, 8))
    before = await container.redis.xlen(stream)
    async with container.uow(TENANT_A) as uow:
        uow.publish(_event(aggregate))
        assert await container.redis.xlen(stream) == before, "внутри транзакции в стриме ничего нет"
    row = await owner.fetchrow("SELECT status::text AS status FROM outbox WHERE aggregate_id = $1", aggregate)
    assert row is not None and row["status"] == "PENDING"
    assert await container.redis.xlen(stream) == before, "до publisher'а в стриме ничего нет"
    publisher = OutboxPublisher(
        container.database, container.redis, maintenance_dsn=maintenance_dsn(container.settings), batch_size=500
    )
    try:
        while await publisher.publish_batch():
            pass
    finally:
        await publisher.release()
    assert await container.redis.xlen(stream) >= before + 1
    assert await owner.fetchval("SELECT status::text FROM outbox WHERE aggregate_id = $1", aggregate) == "PUBLISHED"
