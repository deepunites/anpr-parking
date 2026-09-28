"""Fake-реализации портов для тестов (CORE §7: только в tests/)."""

from __future__ import annotations

from collections import deque
from uuid import UUID

from parking.modules.devices.application.ports import BarrierInfo, DriverResult
from parking.modules.devices.infrastructure.drivers import DriverRegistry


class FakeBarrierDriver:
    """Отдаёт заранее заданные результаты; последний результат «залипает» (пустой список — всегда успех)."""

    def __init__(self, results: list[bool] | None = None) -> None:
        self._results: deque[bool] = deque(results or [])
        self._last = True
        self.calls: list[tuple[str, UUID]] = []

    def _next(self) -> DriverResult:
        if self._results:
            self._last = self._results.popleft()
        ok = self._last
        return DriverResult(ok=ok, error=None if ok else "controller timeout", latency_ms=3)

    async def open(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        self.calls.append(("open", command_id))
        return self._next()

    async def close(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        self.calls.append(("close", command_id))
        return self._next()

    async def probe(self, barrier: BarrierInfo) -> DriverResult:
        result = self._next()
        return DriverResult(ok=result.ok, error=result.error, latency_ms=1, detail="fake")


def fake_registry(driver: FakeBarrierDriver | None = None) -> tuple[DriverRegistry, FakeBarrierDriver]:
    driver = driver or FakeBarrierDriver()
    return DriverRegistry(enabled=True, drivers={"MODBUS_TCP": driver, "HTTP_REST": driver}), driver


async def no_sleep(_: float) -> None:
    """Бэкофф исполнителя без реального ожидания."""


class FakeGateway:
    """Провайдер для тестов: поведение create/status/refund задаётся полями; считает вызовы (двойного create быть не должно)."""

    def __init__(self) -> None:
        from parking.modules.billing.domain.reconciliation import RemoteState

        self.create_mode = "ok"  # ok | timeout | error
        self.status_state: object = RemoteState.PENDING  # RemoteState | None (неизвестно)
        self.refund_ok = True
        self.registry_items: list[object] = []
        self.create_calls = 0
        self.status_calls = 0
        self.refund_calls = 0
        self.created: list[object] = []

    async def create(self, req: object) -> object:
        from parking.modules.billing.application.ports import GatewayError, GatewayTimeoutError, PaymentIntent
        from parking.modules.billing.domain.reconciliation import RemoteState

        self.create_calls += 1
        self.created.append(req)
        if self.create_mode == "timeout":
            raise GatewayTimeoutError("fake: read timeout")
        if self.create_mode == "error":
            raise GatewayError("fake: error -31008")
        return PaymentIntent(
            provider_transaction_id=f"fake-tx-{self.create_calls}",
            checkout_url="https://checkout.fake/pay/1",
            state=RemoteState.PENDING,
        )

    async def status(self, provider_tx_id: str | None, *, idempotency_key: str) -> object:
        from parking.modules.billing.application.ports import PaymentStatusResult

        self.status_calls += 1
        state = self.status_state
        return PaymentStatusResult(state=state, provider_transaction_id=provider_tx_id or "fake-tx-status", amount=None)  # type: ignore[arg-type]

    async def refund(self, req: object) -> object:
        from parking.modules.billing.application.ports import RefundResult

        self.refund_calls += 1
        return RefundResult(
            ok=self.refund_ok,
            provider_refund_id="fake-refund-1" if self.refund_ok else None,
            error=None if self.refund_ok else "fake: declined",
        )

    async def registry(self, start: object, end: object) -> list[object]:
        return list(self.registry_items)


class FakeGatewayRegistry:
    def __init__(self, gateway: FakeGateway | None = None) -> None:
        self.gateway = gateway or FakeGateway()

    def gateway_for(self, account: object) -> FakeGateway:
        return self.gateway
