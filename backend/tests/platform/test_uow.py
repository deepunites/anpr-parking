"""I-5: событие пишется в outbox в той же транзакции и исчезает при откате. I-9: HTTP внутри UoW запрещён."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from parking.platform.boundaries import TransactionBoundaryViolation, in_transaction, transaction_depth
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.http import make_async_client
from tests.conftest import TENANT_A, TENANT_B


@pytest.fixture
async def critical_factory(test_dsn: str) -> async_sessionmaker[AsyncSession]:
    url = test_dsn.replace("postgresql://postgres@", "postgresql+asyncpg://app_critical@", 1)
    engine = create_async_engine(url, poolclass=None)
    yield async_sessionmaker(engine, expire_on_commit=False)  # type: ignore[misc]
    await engine.dispose()


def _event(tenant_id=TENANT_A) -> IntegrationEvent:  # type: ignore[no-untyped-def]
    session_id = uuid4()
    return IntegrationEvent(
        type="parking.session.created.v1",
        tenant_id=tenant_id,
        aggregate_type="parking_session",
        aggregate_id=session_id,
        payload={
            "session_id": str(session_id),
            "lot_id": str(uuid4()),
            "vehicle_id": str(uuid4()),
            "entry_time": "2026-09-19T12:00:00+05:00",
            "status": "ACTIVE",
        },
    )


async def test_publish_rejects_unknown_type_and_bad_payload(critical_factory: async_sessionmaker[AsyncSession]) -> None:
    from parking.platform.events_registry import EventSchemaError, UnknownEventTypeError

    async with UnitOfWork(critical_factory, TENANT_A) as uow:
        with pytest.raises(UnknownEventTypeError):
            uow.publish(
                IntegrationEvent(
                    type="parking.session.exploded.v1",
                    tenant_id=TENANT_A,
                    aggregate_type="x",
                    aggregate_id=uuid4(),
                    payload={},
                )
            )
        with pytest.raises(EventSchemaError, match="session_id"):
            uow.publish(
                IntegrationEvent(
                    type="parking.session.created.v1",
                    tenant_id=TENANT_A,
                    aggregate_type="x",
                    aggregate_id=uuid4(),
                    payload={"n": 1},
                )
            )


async def test_event_is_written_in_same_transaction(
    critical_factory: async_sessionmaker[AsyncSession], owner: asyncpg.Connection
) -> None:
    event = _event()
    async with UnitOfWork(critical_factory, TENANT_A) as uow:
        uow.publish(event)
        assert in_transaction()
        assert await owner.fetchval("SELECT count(*) FROM outbox WHERE event_id = $1", event.event_id) == 0, (
            "до коммита не видно"
        )
    assert not in_transaction()
    row = await owner.fetchrow(
        "SELECT event_type, tenant_id, payload, status::text AS status FROM outbox WHERE event_id = $1", event.event_id
    )
    assert row is not None and row["event_type"] == "parking.session.created.v1" and row["status"] == "PENDING"
    assert row["tenant_id"] == TENANT_A


async def test_rollback_drops_event_and_data(
    critical_factory: async_sessionmaker[AsyncSession], owner: asyncpg.Connection
) -> None:
    event = _event()
    with pytest.raises(RuntimeError, match="boom"):
        async with UnitOfWork(critical_factory, TENANT_A) as uow:
            await uow.session.execute(
                text("INSERT INTO holiday_calendar (id, tenant_id, name) VALUES (:id, :tenant, 'rollback test')"),
                {"id": event.aggregate_id, "tenant": TENANT_A},
            )
            uow.publish(event)
            raise RuntimeError("boom")
    assert await owner.fetchval("SELECT count(*) FROM outbox WHERE event_id = $1", event.event_id) == 0
    assert await owner.fetchval("SELECT count(*) FROM holiday_calendar WHERE id = $1", event.aggregate_id) == 0
    assert transaction_depth.get() == 0


async def test_tenant_context_is_set_and_event_tenant_is_checked(
    critical_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with UnitOfWork(critical_factory, TENANT_A) as uow:
        current = await uow.session.execute(text("SELECT app.current_tenant()"))
        assert str(current.scalar_one()) == str(TENANT_A)
        with pytest.raises(ValueError, match="tenant"):
            uow.publish(_event(TENANT_B))


async def test_http_inside_uow_raises_boundary_violation(critical_factory: async_sessionmaker[AsyncSession]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    client = make_async_client(transport=httpx.MockTransport(handler))
    async with UnitOfWork(critical_factory, TENANT_A):
        with pytest.raises(TransactionBoundaryViolation, match="I-9"):
            await client.get("http://barrier.local/open")
    assert calls == [], "запрос не должен был уйти"
    response = await client.get("http://barrier.local/open")
    assert response.status_code == 200 and calls == ["http://barrier.local/open"]
    await client.aclose()


async def test_nested_depth_counter() -> None:
    token = transaction_depth.set(1)
    try:
        assert in_transaction()
    finally:
        transaction_depth.reset(token)
    assert not in_transaction()
