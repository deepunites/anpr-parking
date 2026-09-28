"""Запрос команды (в транзакции вызывающего) и исполнитель (вне транзакции)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from tenacity import AsyncRetrying, RetryError, retry_if_result, stop_after_attempt, wait_exponential, wait_random

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.devices.application.contracts import BarrierCommandResult
from parking.modules.devices.application.ports import (
    BarrierCommandRepository,
    BarrierInfo,
    BarrierInfoCache,
    BarrierRepository,
    DriverRegistry,
    DriverResult,
)
from parking.modules.devices.domain.command import BarrierCommand, CommandKind, CommandSource, CommandState
from parking.platform import metrics
from parking.platform.breaker import BreakerRegistry, BreakerState, CircuitOpenError
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.platform.tracing import FLOW_ATTR, span
from parking.shared.clock import Clock
from parking.shared.correlation import get_correlation_id
from parking.shared.errors import NotFoundError, ServiceUnavailableError

log = get_logger("devices.barrier")


@dataclass(frozen=True, slots=True)
class DeviceRepositories:
    barriers: BarrierRepository
    commands: BarrierCommandRepository
    audit: AuditWriter


class SqlBarrierCommands:
    """Реализация контракта BarrierCommands поверх открытой UoW."""

    def __init__(self, uow: UnitOfWork, repos: DeviceRepositories, clock: Clock) -> None:
        self._uow = uow
        self._repos = repos
        self._clock = clock

    async def request_open(
        self,
        *,
        lot_id: UUID,
        camera_id: UUID | None,
        session_id: UUID | None,
        saga_id: UUID | None,
        source: CommandSource,
        idempotency_key: bytes,
        issued_by: UUID | None = None,
        reason: str | None = None,
        fresh_key: bool = False,
    ) -> BarrierCommandResult:
        if not fresh_key:  # ключ из только что созданного агрегата (ENTRY: session.id) повториться не может
            existing = await self._repos.commands.find_by_key(idempotency_key)
            if existing is not None:
                return BarrierCommandResult(existing.id, existing.barrier_id, existing.state)
        barrier = await self._repos.barriers.for_camera(lot_id, camera_id)
        if barrier is None:
            log.info("barrier_absent", lot_id=str(lot_id), camera_id=str(camera_id) if camera_id else None)
            return BarrierCommandResult(None, None, CommandState.SKIPPED)
        tenant_id = self._uow.tenant_id
        assert tenant_id is not None
        command = BarrierCommand(
            id=uuid4(),
            tenant_id=tenant_id,
            lot_id=lot_id,
            barrier_id=barrier.id,
            kind=CommandKind.OPEN,
            source=source,
            idempotency_key=idempotency_key,
            correlation_id=UUID(get_correlation_id()),
            created_at=self._clock.now(),
            session_id=session_id,
            saga_id=saga_id,
            issued_by=issued_by,
            reason=reason,
        )
        await self._repos.commands.add(command)
        if source is CommandSource.MANUAL:
            await self._repos.audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    action="BARRIER_OPEN_MANUAL",
                    entity_type="barrier",
                    entity_id=barrier.id,
                    lot_id=lot_id,
                    session_id=session_id,
                    actor_type=ActorType.USER,
                    actor_id=issued_by,
                    user_id=issued_by,
                    reason=reason,
                    payload={"command_id": str(command.id)},
                )
            )
        self._uow.publish(
            IntegrationEvent(
                type="devices.barrier.command_requested.v1",
                tenant_id=tenant_id,
                aggregate_type="barrier_command",
                aggregate_id=command.id,
                payload={
                    "command_id": str(command.id),
                    "barrier_id": str(barrier.id),
                    "lot_id": str(lot_id),
                    "kind": "OPEN",
                    "source": source.value,
                    "session_id": str(session_id) if session_id else None,
                    "saga_id": str(saga_id) if saga_id else None,
                },
            )
        )
        return BarrierCommandResult(command.id, barrier.id, command.state)

    async def status(self, command_id: UUID) -> CommandState | None:
        command = await self._repos.commands.get(command_id)
        return None if command is None else command.state


def _log_breaker(name: str, old: BreakerState, new: BreakerState) -> None:
    log.warning("barrier_breaker_state", barrier_id=name, old=old.value, new=new.value)


class BarrierCommandExecutor:
    """Исполняет команду: TX1 PENDING→SENT, вызов драйвера вне транзакции, TX2 результат + событие.

    Надёжность (фаза 4 §3): таймаут драйвера 3 с; OPEN ретраится до `retries` раз с бэкоффом 0.5/1/2 с + джиттер —
    открыть открытое безопасно (no-op); CLOSE не ретраится автоматически — повтор может опустить стрелу на машину.
    Circuit breaker на шлагбаум: 5 отказов → OPEN 60 с → HALF_OPEN одна проба; при открытом breaker команда
    завершается FAILED сразу, без обращения к контроллеру (сага → MANUAL_REVIEW, оператор — вручную).
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], DeviceRepositories],
        drivers: DriverRegistry,
        clock: Clock,
        breakers: BreakerRegistry | None = None,
        retries: int = 3,
        backoff_base: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        barrier_cache: BarrierInfoCache | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._drivers = drivers
        self._clock = clock
        self._barrier_cache = barrier_cache
        self._breakers = breakers or BreakerRegistry(on_state_change=_log_breaker)
        self._retries = max(1, retries)
        self._backoff_base = backoff_base
        self._sleep = sleep

    async def execute(self, tenant_id: UUID, command_id: UUID) -> CommandState:
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            command = await repos.commands.get_for_update(command_id)
            if command is None:
                raise NotFoundError("barrier command not found")
            if command.state is not CommandState.PENDING:
                return command.state  # уже исполнена или в работе — идемпотентно
            barrier = await repos.barriers.get(command.barrier_id)
            command.mark_sent()
            await repos.commands.save(command)
        if barrier is None:
            return await self._finish(tenant_id, command_id, ok=False, error="barrier not found", latency_ms=None)
        await self._remember(barrier)
        started = time.perf_counter()
        result = await self._call_driver(barrier, command, command_id)
        latency = int((time.perf_counter() - started) * 1000)
        # значения result = CommandState: их ждут алерты ExitSuccessRatioLow/BarrierFailure
        metrics.barrier_command_total.labels(
            lot=str(barrier.lot_id), command=command.kind.value, result="OK" if result.ok else "FAILED"
        ).inc()
        return await self._finish(tenant_id, command_id, ok=result.ok, error=result.error, latency_ms=latency)

    async def _remember(self, barrier: BarrierInfo) -> None:
        if self._barrier_cache is None:
            return
        try:
            await self._barrier_cache.remember(barrier)
        except Exception as exc:
            log.warning("barrier_cache_unavailable", error_type=type(exc).__name__)

    async def _call_driver(self, barrier: BarrierInfo, command: BarrierCommand, command_id: UUID) -> DriverResult:
        with span(
            "barrier.open" if command.kind is CommandKind.OPEN else "barrier.close",
            lot_id=barrier.lot_id,
            session_id=command.session_id,
            tenant_id=command.tenant_id,
            **{FLOW_ATTR: "exit" if command.source is CommandSource.AUTO_EXIT else "other"},
        ):
            return await self._call_driver_traced(barrier, command, command_id)

    async def _call_driver_traced(
        self, barrier: BarrierInfo, command: BarrierCommand, command_id: UUID
    ) -> DriverResult:
        try:
            driver = self._drivers.driver_for(barrier)
        except ServiceUnavailableError as exc:
            return DriverResult(ok=False, error=str(exc))
        breaker = self._breakers.get(str(barrier.id))
        attempts = self._retries if command.kind is CommandKind.OPEN else 1

        calls = 0

        async def attempt() -> DriverResult:
            nonlocal calls
            calls += 1
            try:
                breaker.acquire()
            except CircuitOpenError as exc:
                log.warning("barrier_circuit_open", barrier_id=str(barrier.id), retry_after=round(exc.retry_after, 1))
                return DriverResult(ok=False, error=f"circuit open, retry after {exc.retry_after:.0f}s")
            result = (
                await driver.open(barrier, command_id)
                if command.kind is CommandKind.OPEN
                else await driver.close(barrier, command_id)
            )
            if result.ok:
                breaker.record_success()
            else:
                breaker.record_failure()
                log.warning(
                    "barrier_attempt_failed",
                    barrier_id=str(barrier.id),
                    attempt=calls,
                    of=attempts,
                    error=result.error,
                    failures=breaker.failures,
                )
            return result

        retrying = AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=self._backoff_base, min=self._backoff_base, max=self._backoff_base * 4)
            + wait_random(0, self._backoff_base / 2),
            retry=retry_if_result(lambda r: not r.ok and not str(r.error).startswith("circuit open")),
            sleep=self._sleep,
        )
        try:
            return await retrying(attempt)
        except RetryError as exc:
            outcome = exc.last_attempt.result()
            return outcome if isinstance(outcome, DriverResult) else DriverResult(ok=False, error="retries exhausted")

    async def _finish(
        self, tenant_id: UUID, command_id: UUID, *, ok: bool, error: str | None, latency_ms: int | None
    ) -> CommandState:
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            command = await repos.commands.get_for_update(command_id)
            assert command is not None
            command.complete(ok=ok, error=error, latency_ms=latency_ms)
            await repos.commands.save(command)
            uow.publish(
                IntegrationEvent(
                    type="devices.barrier.command_completed.v1",
                    tenant_id=tenant_id,
                    aggregate_type="barrier_command",
                    aggregate_id=command.id,
                    payload={
                        "command_id": str(command.id),
                        "barrier_id": str(command.barrier_id),
                        "lot_id": str(command.lot_id),
                        "result": command.state.value,
                        "session_id": str(command.session_id) if command.session_id else None,
                        "saga_id": str(command.saga_id) if command.saga_id else None,
                        "error": error,
                    },
                )
            )
            log.info(
                "barrier_command_completed",
                command_id=str(command.id),
                result=command.state.value,
                latency_ms=latency_ms,
            )
            return command.state
