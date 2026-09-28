"""Обработчики integration events для Stream Consumer. Каждый обработчик идемпотентен (at-least-once)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from parking.modules.integrations.application.contracts import EVENT_CATALOG
from parking.platform.logging import get_logger
from parking.runtime.container import Container
from parking.shared.correlation import correlation_scope
from parking.workers.envelope import Envelope

log = get_logger("consumer.handlers")

Handler = Callable[[Envelope], Awaitable[None]]


class BarrierDispatcher(Protocol):
    async def dispatch(self, tenant_id: UUID, command_id: UUID) -> None:
        """Передаёт исполнение команды шлагбауму в очередь barrier (Celery) либо выполняет сразу (inline-режим)."""
        ...


class InlineBarrierDispatcher:
    """Исполняет команду в процессе потребителя (dev/тесты; PARKING_BARRIER_DISPATCH=inline)."""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def dispatch(self, tenant_id: UUID, command_id: UUID) -> None:
        await self._container.barrier_executor.execute(tenant_id, command_id)


class CeleryBarrierDispatcher:
    """Ставит команду в очередь barrier (prod, PARKING_BARRIER_DISPATCH=celery): исполнение изолировано от потребителя."""

    async def dispatch(self, tenant_id: UUID, command_id: UUID) -> None:
        from parking.workers.celery_app import execute_barrier_command  # noqa: PLC0415

        execute_barrier_command.apply_async(args=(str(tenant_id), str(command_id)), queue="barrier")


def build_handlers(container: Container, barriers: BarrierDispatcher) -> dict[str, Handler]:
    parking = container.parking

    async def advance_saga(env: Envelope) -> None:
        saga_id = env.payload.get("saga_id")
        if not saga_id:
            return
        with correlation_scope(env.correlation_id, tenant_id=env.tenant_id):
            state = await parking.advance_saga(env.tenant_id, UUID(str(saga_id)))
        log.info("saga_advanced", saga_id=str(saga_id), state=state.value, trigger=env.event_type)

    async def barrier_requested(env: Envelope) -> None:
        with correlation_scope(env.correlation_id, tenant_id=env.tenant_id):
            await barriers.dispatch(env.tenant_id, UUID(str(env.payload["command_id"])))

    async def camera_key_revoked(env: Envelope) -> None:
        await container.camera_auth.invalidate(str(env.payload["key_prefix"]))

    async def access_changed(env: Envelope) -> None:
        plate_hash = bytes.fromhex(str(env.payload["plate_hash"]))
        container.access_resolver._local.invalidate(env.tenant_id, plate_hash)
        await container.access._cache.invalidate(env.tenant_id, plate_hash)

    return {
        "parking.exit.requested.v1": advance_saga,
        "billing.payment.confirmed.v1": advance_saga,
        "billing.payment.failed.v1": advance_saga,
        "billing.payment.unverified.v1": advance_saga,
        "devices.barrier.command_completed.v1": advance_saga,
        "devices.barrier.command_requested.v1": barrier_requested,
        "access.rule.changed.v1": access_changed,
        "devices.camera_key.revoked.v1": camera_key_revoked,
    }


def with_webhooks(handlers: dict[str, Handler], container: Container) -> dict[str, Handler]:
    """v2 этап 6: события из каталога вебхуков → доставки подписчикам клиента (после доменного обработчика).
    Отправляет их диспетчер (main_webhooks) — стрим не ждёт чужих серверов."""
    combined: dict[str, Handler] = dict(handlers)
    for event_type in EVENT_CATALOG.values():
        inner = handlers.get(event_type)

        async def run(env: Envelope, _inner: Handler | None = inner) -> None:
            if _inner is not None:
                await _inner(env)
            with correlation_scope(env.correlation_id, tenant_id=env.tenant_id):
                await container.outgoing_webhooks.enqueue(
                    env.tenant_id, env.event_type, env.event_id, env.occurred_at, env.payload
                )

        combined[event_type] = run
    return combined
