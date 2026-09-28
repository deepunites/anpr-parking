"""Возвраты (фаза 5 §4): только со ссылкой на PAID-платёж, сумма ≤ платёж − уже возвращённое (проверка в коде и
триггером БД под блокировкой платежа), частичные допустимы, permission payment:refund + reason + аудит.
Наличные — с привязкой к открытой смене кассира; CARD/ONLINE — через провайдера вне транзакции, ретрай запрещён."""

from __future__ import annotations

import time
from uuid import UUID, uuid4

from parking.modules.billing.application.common import (
    BillingRepositories,
    ReposFactory,
    UowFactory,
    audit,
    check_lot_scope,
    refund_event,
)
from parking.modules.billing.application.ports import GatewayRegistry, GatewayTimeoutError, RefundRequest
from parking.modules.billing.domain.payment import (
    Payment,
    PaymentMethod,
    Refund,
    RefundStatus,
    check_refund,
    payment_key,
)
from parking.modules.identity.application.contracts import CurrentUser
from parking.platform import metrics
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import ConflictError, NotFoundError, ServiceUnavailableError, ValidationError

log = get_logger("billing.refund")


class RefundService:
    def __init__(
        self, *, uow_factory: UowFactory, repos: ReposFactory, gateways: GatewayRegistry, clock: Clock
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._gateways = gateways
        self._clock = clock

    async def request(
        self, actor: CurrentUser, payment_id: UUID, *, amount: int, reason: str, idempotency_key: str | None
    ) -> Refund:
        key = payment_key("refund", payment_id, idempotency_key or f"{amount}:{reason}:{self._clock.now():%Y%m%d%H%M}")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            existing = await repos.refunds.find_by_key(key)
            if existing is not None:
                return existing
            payment = await repos.payments.get(payment_id, for_update=True)
            if payment is None or not check_lot_scope(actor, payment.lot_id):
                raise NotFoundError("payment not found")
            refunds = await repos.refunds.list_for_payment(payment.id)
            check_refund(payment, amount, [r.amount for r in refunds if r.status is not RefundStatus.FAILED])
            if payment.method is None or payment.method not in {
                PaymentMethod.CASH,
                PaymentMethod.CARD,
                PaymentMethod.ONLINE,
            }:
                raise ValidationError("only CASH/CARD/ONLINE payments are refundable")
            refund = Refund(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                payment_id=payment.id,
                amount=amount,
                method=payment.method,
                operator_id=actor.user_id,
                reason=reason,
                idempotency_key=key,
            )
            if payment.method is PaymentMethod.CASH:
                shift = await repos.shifts.open_for_cashier(actor.user_id)
                if shift is None or shift.lot_id != payment.lot_id:
                    raise ConflictError(
                        "cash refund requires the operator's open shift on the payment's lot", code="NO_OPEN_SHIFT"
                    )
                refund.shift_id = shift.id
                refund.complete(now=self._clock.now())  # деньги выданы из кассы здесь и сейчас
            await repos.refunds.add(refund)  # триггер refund_guard: сумма возвратов ≤ платёж под FOR UPDATE
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action="REFUND_REQUESTED" if refund.status is RefundStatus.PENDING else "REFUND_CASH",
                entity_type="refund",
                entity_id=refund.id,
                actor=actor,
                lot_id=payment.lot_id,
                session_id=payment.session_id,
                reason=reason,
                payload={"payment_id": str(payment.id), "amount": amount, "method": payment.method.value},
            )
            uow.publish(refund_event(refund, "completed" if refund.status is RefundStatus.DONE else "created"))
            metrics.refund_total.labels(method=payment.method.value, result=refund.status.value.lower()).inc()
        return refund

    async def list_for_payment(self, actor: CurrentUser, payment_id: UUID) -> list[Refund]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            payment = await repos.payments.get(payment_id)
            if payment is None or not check_lot_scope(actor, payment.lot_id):
                raise NotFoundError("payment not found")
            return await repos.refunds.list_for_payment(payment_id)

    async def execute_provider_refund(self, tenant_id: UUID, refund_id: UUID) -> RefundStatus:
        """TX1: PENDING → блокировка и данные; вызов провайдера вне TX (без ретрая); TX2: DONE/FAILED + событие."""
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            refund = await repos.refunds.get(refund_id, for_update=True)
            if refund is None:
                raise NotFoundError("refund not found")
            if refund.status is not RefundStatus.PENDING or refund.method is PaymentMethod.CASH:
                return refund.status
            payment = await repos.payments.get(refund.payment_id)
            attempt = await repos.attempts.latest(refund.payment_id)
            account = await repos.accounts.for_tenant(payment.provider) if payment else None
        if payment is None or attempt is None or attempt.provider_transaction_id is None or account is None:
            return await self._finish(
                tenant_id, refund_id, ok=False, provider_refund_id=None, error="no provider transaction to refund"
            )
        gateway = self._gateways.gateway_for(account)
        started = time.perf_counter()
        try:
            result = await gateway.refund(
                RefundRequest(
                    provider_transaction_id=attempt.provider_transaction_id,
                    amount=refund.amount,
                    idempotency_key=str(refund.id),
                    reason=refund.reason,
                )
            )
        except GatewayTimeoutError:
            log.error("refund_provider_timeout", refund_id=str(refund.id), provider=payment.provider.value)
            raise ServiceUnavailableError("provider did not answer; refund stays PENDING for status check") from None
        log.info(
            "refund_provider_result",
            refund_id=str(refund.id),
            ok=result.ok,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return await self._finish(
            tenant_id, refund_id, ok=result.ok, provider_refund_id=result.provider_refund_id, error=result.error
        )

    async def _finish(
        self, tenant_id: UUID, refund_id: UUID, *, ok: bool, provider_refund_id: str | None, error: str | None
    ) -> RefundStatus:
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            refund = await repos.refunds.get(refund_id, for_update=True)
            assert refund is not None
            if refund.status is not RefundStatus.PENDING:
                return refund.status
            if ok:
                refund.complete(now=self._clock.now(), provider_refund_id=provider_refund_id)
            else:
                refund.fail()
            await repos.refunds.save(refund)
            await audit(
                repos,
                tenant_id=tenant_id,
                action="REFUND_DONE" if ok else "REFUND_FAILED",
                entity_type="refund",
                entity_id=refund.id,
                actor=None,
                reason=error,
                payload={"payment_id": str(refund.payment_id), "amount": refund.amount},
            )
            uow.publish(refund_event(refund, "completed" if ok else "failed"))
            metrics.refund_total.labels(method=refund.method.value, result="done" if ok else "failed").inc()
            return refund.status

    async def process_pending(self, tenant_id: UUID, *, limit: int = 50) -> int:
        async with self._uow_factory(tenant_id) as uow:
            pending = await self._repos(uow).refunds.pending_provider(limit=limit)
        done = 0
        for refund in pending:
            try:
                if await self.execute_provider_refund(tenant_id, refund.id) is RefundStatus.DONE:
                    done += 1
            except ServiceUnavailableError:
                break
        return done


def refund_is_possible(payment: Payment, refunds: list[Refund]) -> int:
    return payment.amount - sum(r.amount for r in refunds if r.status is not RefundStatus.FAILED)


__all__ = ["BillingRepositories", "RefundService", "refund_is_possible"]
