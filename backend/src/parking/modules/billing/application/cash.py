"""Наличные и смены кассиров (фаза 5 §3 CASH). Сумма считается на бэкенде; присланная кассиром — только сверка
(§1 п.5): расхождение — отказ, нестандартная сумма — только с `payment:create_cash_custom` и reason.
Наличные без смены не сверяются: платёж CASH обязан ссылаться на открытую смену кассира на этой площадке
(ck_cash_has_operator_shift)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from parking.modules.billing.application.common import (
    BillingRepositories,
    ReposFactory,
    UowFactory,
    audit,
    check_lot_scope,
    payment_confirmed,
    record_paid,
)
from parking.modules.billing.domain.payment import (
    AttemptStatus,
    CashierShift,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    Provider,
    amount_matches,
    payment_key,
)
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.parking.application.contracts import ParkingCommands
from parking.platform import metrics
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError

log = get_logger("billing.cash")


class CashierService:
    def __init__(self, *, uow_factory: UowFactory, repos: ReposFactory, parking: ParkingCommands, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._parking = parking
        self._clock = clock

    # ---------- смены ----------

    async def open_shift(
        self, actor: CurrentUser, lot_id: UUID, *, opening_float: int, notes: str | None
    ) -> CashierShift:
        if not check_lot_scope(actor, lot_id):
            raise NotFoundError("lot not found")
        if opening_float < 0:
            raise ValidationError("opening float cannot be negative")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")
            if await repos.shifts.open_for_cashier(actor.user_id) is not None:
                raise ConflictError("cashier already has an open shift")
            shift = CashierShift(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                lot_id=lot_id,
                cashier_id=actor.user_id,
                opening_float=opening_float,
                notes=notes,
            )
            await repos.shifts.add(shift)
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action="SHIFT_OPENED",
                entity_type="cashier_shift",
                entity_id=shift.id,
                actor=actor,
                lot_id=lot_id,
                payload={"opening_float": opening_float},
            )
        return shift

    async def close_shift(
        self, actor: CurrentUser, shift_id: UUID, *, counted_cash: int, notes: str | None
    ) -> CashierShift:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            shift = await repos.shifts.get(shift_id, for_update=True)
            if shift is None or not check_lot_scope(actor, shift.lot_id):
                raise NotFoundError("shift not found")
            if shift.cashier_id != actor.user_id and not actor.has(Permission.SHIFT_MANAGE):
                raise AuthorizationError("only the cashier or a manager can close the shift")
            cash_in, cash_out = await repos.shifts.cash_totals(shift.id)
            shift.close(
                now=self._clock.now(),
                cash_in=cash_in,
                cash_out=cash_out,
                counted_cash=counted_cash,
                by=actor.user_id,
                notes=notes,
            )
            await repos.shifts.save(shift)
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action="SHIFT_CLOSED",
                entity_type="cashier_shift",
                entity_id=shift.id,
                actor=actor,
                lot_id=shift.lot_id,
                reason=notes,
                payload={
                    "expected_cash": shift.expected_cash,
                    "counted_cash": counted_cash,
                    "discrepancy": shift.discrepancy,
                },
            )
            uow.publish(
                IntegrationEvent(
                    type="billing.shift.closed.v1",
                    tenant_id=actor.tenant_id,
                    aggregate_type="cashier_shift",
                    aggregate_id=shift.id,
                    payload={
                        "shift_id": str(shift.id),
                        "lot_id": str(shift.lot_id),
                        "cashier_id": str(shift.cashier_id),
                        "expected_cash": shift.expected_cash or 0,
                        "counted_cash": counted_cash,
                        "discrepancy": shift.discrepancy or 0,
                    },
                )
            )
            if shift.discrepancy:
                log.warning("shift_cash_discrepancy", shift_id=str(shift.id), discrepancy=shift.discrepancy)
        return shift

    async def current_shift(self, actor: CurrentUser) -> CashierShift | None:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).shifts.open_for_cashier(actor.user_id)

    async def get_shift(self, actor: CurrentUser, shift_id: UUID) -> CashierShift:
        async with self._uow_factory(actor.tenant_id) as uow:
            shift = await self._repos(uow).shifts.get(shift_id)
        if shift is None or not check_lot_scope(actor, shift.lot_id):
            raise NotFoundError("shift not found")
        return shift

    async def list_shifts(self, actor: CurrentUser, *, limit: int = 50) -> list[CashierShift]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).shifts.list(lot_ids=actor.allowed_lots(), limit=limit)

    # ---------- наличные ----------

    async def confirm_cash(
        self,
        actor: CurrentUser,
        payment_id: UUID,
        *,
        declared_amount: int | None,
        reason: str | None,
        method: PaymentMethod = PaymentMethod.CASH,
    ) -> Payment:
        """PENDING-обязательство саги оплачено наличными или по QR площадки. Повтор для уже оплаченного возвращает
        тот же платёж (двойная отправка → одно списание)."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            payment = await repos.payments.get(payment_id, for_update=True)
            if payment is None or not check_lot_scope(actor, payment.lot_id):
                raise NotFoundError("payment not found")
            if payment.status is PaymentStatus.PAID:
                return payment
            if payment.status is not PaymentStatus.PENDING:
                raise ConflictError(f"payment is {payment.status.value}")
            shift = await self._open_shift_for(repos, actor, payment.lot_id)
            amount = self._settle_amount(actor, payment.amount, declared_amount, reason)
            if amount != payment.amount:
                raise ValidationError(  # обязательство создано сагой: сумму не переписываем (I-6/I-7), только refund
                    "custom amount is not allowed for an existing obligation; refund the difference instead"
                )
            now = self._clock.now()
            payment.confirm_cash(operator_id=actor.user_id, shift_id=shift.id, now=now, paid_until=None, method=method)
            await repos.payments.save(payment)
            await self._cash_attempt(repos, payment, shift.id, actor.user_id)
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action=f"PAYMENT_{method.value}",
                entity_type="payment",
                entity_id=payment.id,
                actor=actor,
                lot_id=payment.lot_id,
                session_id=payment.session_id,
                reason=reason,
                payload={"amount": payment.amount, "shift_id": str(shift.id), "declared_amount": declared_amount},
            )
            uow.publish(payment_confirmed(payment))
            metrics.payment_total.labels(method=method.value, status="PAID").inc()
            record_paid(payment)
        return payment

    async def prepay(
        self,
        actor: CurrentUser,
        session_id: UUID,
        *,
        declared_amount: int | None,
        reason: str | None,
        method: PaymentMethod = PaymentMethod.CASH,
    ) -> Payment:
        """Предоплата до появления машины у выезда (A-11, SD-2b): PAID сразу, paid_until = now + exit_grace."""
        now = self._clock.now()
        quote = await self._parking.prepay_quote(actor.tenant_id, session_id, now)
        if not check_lot_scope(actor, quote.lot_id):
            raise NotFoundError("session not found")
        amount = self._settle_amount(actor, quote.amount_minor, declared_amount, reason)
        key = payment_key("prepay", session_id, quote.amount_minor, now.replace(second=0, microsecond=0).isoformat())
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            existing = await repos.payments.find_by_key(key)
            if existing is not None:
                return existing  # двойная отправка в ту же минуту — одно списание
            for paid in await repos.payments.list_for_session(session_id):
                if paid.status is PaymentStatus.PAID and paid.paid_until is not None and paid.paid_until >= now:
                    return paid  # окно выезда ещё открыто: второй раз платить не за что
            shift = await self._open_shift_for(repos, actor, quote.lot_id)
            snapshot = dict(quote.snapshot)
            if amount != quote.amount_minor:
                snapshot["custom_amount"] = {"declared": amount, "computed": quote.amount_minor, "reason": reason}
            payment = Payment(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                lot_id=quote.lot_id,
                session_id=session_id,
                amount=amount,
                status=PaymentStatus.PENDING,
                idempotency_key=key,
                calculation_snapshot=snapshot,
                source_id=quote.source_id,
            )
            payment.confirm_cash(
                operator_id=actor.user_id,
                shift_id=shift.id,
                now=now,
                paid_until=now + timedelta(minutes=quote.exit_grace_minutes),
                method=method,
            )
            await repos.payments.add(payment)
            await self._cash_attempt(repos, payment, shift.id, actor.user_id)
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action=f"PAYMENT_{method.value}_PREPAID",
                entity_type="payment",
                entity_id=payment.id,
                actor=actor,
                lot_id=payment.lot_id,
                session_id=session_id,
                reason=reason,
                payload={
                    "amount": amount,
                    "computed": quote.amount_minor,
                    "shift_id": str(shift.id),
                    "paid_until": payment.paid_until.isoformat() if payment.paid_until else None,
                },
            )
            uow.publish(payment_confirmed(payment))
            metrics.payment_total.labels(method=method.value, status="PREPAID").inc()
            record_paid(payment)
        return payment

    async def get_payment(self, actor: CurrentUser, payment_id: UUID) -> Payment:
        async with self._uow_factory(actor.tenant_id) as uow:
            payment = await self._repos(uow).payments.get(payment_id)
        if payment is None or not check_lot_scope(actor, payment.lot_id):
            raise NotFoundError("payment not found")
        return payment

    async def session_payments(self, actor: CurrentUser, session_id: UUID) -> list[Payment]:
        async with self._uow_factory(actor.tenant_id) as uow:
            payments = await self._repos(uow).payments.list_for_session(session_id)
        return [p for p in payments if check_lot_scope(actor, p.lot_id)]

    async def quote(self, actor: CurrentUser, session_id: UUID) -> int:
        quote = await self._parking.prepay_quote(actor.tenant_id, session_id, self._clock.now())
        if not check_lot_scope(actor, quote.lot_id):
            raise NotFoundError("session not found")
        return quote.amount_minor

    # ---------- общее ----------

    @staticmethod
    def _settle_amount(actor: CurrentUser, computed: int, declared: int | None, reason: str | None) -> int:
        if amount_matches(computed, declared):
            return computed
        assert declared is not None
        if not actor.has(Permission.PAYMENT_CREATE_CASH_CUSTOM):
            metrics.payment_total.labels(method="CASH", status="AMOUNT_MISMATCH").inc()
            raise ValidationError(
                "declared amount differs from the computed one", code="AMOUNT_MISMATCH", details={"computed": computed}
            )
        if not reason or len(reason.strip()) < 3:
            raise ValidationError("custom amount requires a reason", code="AMOUNT_MISMATCH")
        if declared < 0:
            raise ValidationError("amount cannot be negative")
        return declared

    @staticmethod
    async def _open_shift_for(repos: BillingRepositories, actor: CurrentUser, lot_id: UUID) -> CashierShift:
        shift = await repos.shifts.open_for_cashier(actor.user_id)
        if shift is None:
            raise ConflictError("cashier has no open shift", code="NO_OPEN_SHIFT")
        if shift.lot_id != lot_id:
            raise ConflictError("open shift belongs to another lot", code="SHIFT_LOT_MISMATCH")
        return shift

    async def _cash_attempt(
        self, repos: BillingRepositories, payment: Payment, shift_id: UUID, operator_id: UUID
    ) -> None:
        now = self._clock.now()
        await repos.attempts.add(
            PaymentAttempt(
                id=uuid4(),
                tenant_id=payment.tenant_id,
                payment_id=payment.id,
                attempt_no=await repos.attempts.next_no(payment.id),
                method=payment.method or PaymentMethod.CASH,
                provider=Provider.NONE,
                request_id=uuid4(),
                status=AttemptStatus.SUCCEEDED,
                request_payload={"amount": payment.amount, "currency": payment.currency},
                shift_id=shift_id,
                operator_id=operator_id,
                started_at=now,
                finished_at=now,
                latency_ms=0,
            )
        )
