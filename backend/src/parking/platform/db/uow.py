"""Unit of Work: одна транзакция, tenant-контекст для RLS, outbox в той же транзакции (I-5), граница HTTP (I-9)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.platform.boundaries import transaction_depth
from parking.platform.events import IntegrationEvent
from parking.platform.events_registry import validate_event
from parking.platform.logging import get_logger
from parking.platform.tracing import current_traceparent
from parking.shared.correlation import get_correlation_id

log = get_logger("uow")

_OUTBOX_INSERT = text(
    """
    INSERT INTO outbox (event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload,
                        correlation_id, trace_parent)
    VALUES (:event_id, :tenant_id, :aggregate_type, :aggregate_id, :event_type, CAST(:payload AS jsonb),
            :correlation_id, :trace_parent)
    """
)


class UnitOfWork:
    """`async with UnitOfWork(factory, tenant_id) as uow:` — коммит при выходе без исключения, иначе откат.

    Внутри блока `transaction_depth > 0`: исходящий HTTP через `platform.http` бросает TransactionBoundaryViolation.
    Бросает исключения SQLAlchemy/asyncpg как есть.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: UUID | None,
        *,
        trace_parent: str | None = None,
    ) -> None:
        self._factory = session_factory
        self.tenant_id = tenant_id
        self._trace_parent = trace_parent
        self._events: list[IntegrationEvent] = []
        self._after_commit: list[Callable[[], Awaitable[None]]] = []
        self._session: AsyncSession | None = None
        self._depth_token: object = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not entered")
        return self._session

    @property
    def pending_events(self) -> tuple[IntegrationEvent, ...]:
        return tuple(self._events)

    def publish(self, *events: IntegrationEvent) -> None:
        """Ставит интеграционные события в очередь на запись в outbox при коммите."""
        for event in events:
            if self.tenant_id is not None and event.tenant_id != self.tenant_id:
                raise ValueError("event tenant differs from UoW tenant")
            validate_event(event.type, event.payload)  # контракт проверяется у продюсера, не у потребителя
        self._events.extend(events)

    def after_commit(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Побочный эффект после успешного коммита (инвалидация кеша, постановка задачи).

        Ошибка хука логируется, не бросается: надёжную доставку обеспечивает outbox-событие, хук — только ускорение.
        """
        self._after_commit.append(hook)

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        await self._session.begin()
        if self.tenant_id is not None:
            await self._session.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant, true)"), {"tenant": str(self.tenant_id)}
            )
        self._depth_token = transaction_depth.set(transaction_depth.get() + 1)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        session = self.session
        committed = False
        try:
            if exc_type is None:
                await self._flush_events()
                await session.commit()
                committed = True
            else:
                await session.rollback()
        finally:
            transaction_depth.reset(self._depth_token)  # type: ignore[arg-type]
            await session.close()
            self._session = None
        if committed:
            for hook in self._after_commit:
                try:
                    await hook()
                except Exception as hook_error:
                    log.warning("after_commit_hook_failed", error_type=type(hook_error).__name__)
            self._after_commit.clear()

    async def _flush_events(self) -> None:
        if not self._events:
            return
        correlation_id = get_correlation_id()
        trace_parent = self._trace_parent or current_traceparent()  # W3C-контекст едет с событием
        rows = [
            {
                "event_id": event.event_id,
                "tenant_id": event.tenant_id,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "event_type": event.type,
                "payload": json.dumps(event.payload, default=str),
                "correlation_id": correlation_id,
                "trace_parent": trace_parent,
            }
            for event in self._events
        ]
        # executemany: asyncpg отправляет все строки одним конвейером (Bind/Execute × N + Sync) — один round-trip
        # вместо N; ENTRY публикует 2–3 события.
        await self.session.execute(_OUTBOX_INSERT, rows)
        self._events.clear()
