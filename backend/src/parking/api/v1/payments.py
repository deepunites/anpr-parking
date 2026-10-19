"""Платежи (фаза 5): смены кассиров, наличные (подтверждение обязательства и предоплата), онлайн-checkout,
возвраты, вебхуки провайдеров, сверка. Сумма всегда считается на бэкенде; `amount_minor` в запросах — сверка."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, Field

from parking.api.deps import PUBLIC, get_container, permission_extra, require
from parking.api.schemas import MoneyOut, Reason
from parking.modules.billing.application.contracts import (
    CashierShift,
    Checkout,
    DiscrepancyRecord,
    DiscrepancyStatus,
    Payment,
    PaymentMethod,
    Provider,
    ReconciliationRun,
    Refund,
)
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.runtime.container import Container
from parking.shared.errors import NotFoundError
from parking.shared.money import Money

router = APIRouter(prefix="/api/v1", tags=["payments"])


class ShiftOut(BaseModel):
    id: str
    lot_id: str
    cashier_id: str
    status: str
    opened_at: datetime | None
    closed_at: datetime | None
    opening_float: int
    expected_cash: int | None
    counted_cash: int | None
    discrepancy: int | None
    notes: str | None

    @classmethod
    def of(cls, s: CashierShift) -> ShiftOut:
        return cls(
            id=str(s.id),
            lot_id=str(s.lot_id),
            cashier_id=str(s.cashier_id),
            status=s.status.value,
            opened_at=s.opened_at,
            closed_at=s.closed_at,
            opening_float=s.opening_float,
            expected_cash=s.expected_cash,
            counted_cash=s.counted_cash,
            discrepancy=s.discrepancy,
            notes=s.notes,
        )


class OpenShiftRequest(BaseModel):
    lot_id: UUID
    opening_float: int = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class CloseShiftRequest(BaseModel):
    counted_cash: int = Field(ge=0)
    notes: str | None = Field(default=None, max_length=500)


class PaymentOut(BaseModel):
    id: str
    session_id: str
    lot_id: str
    saga_id: str | None
    amount_minor: int
    amount: MoneyOut  # форматирование суммы — с бэкенда, UI не форматирует деньги сам
    currency: str
    status: str
    method: str | None
    provider: str
    paid_at: datetime | None
    paid_until: datetime | None
    operator_id: str | None
    shift_id: str | None
    cancel_reason: str | None
    calculation_snapshot: dict[str, Any]

    @classmethod
    def of(cls, p: Payment) -> PaymentOut:
        return cls(
            id=str(p.id),
            session_id=str(p.session_id),
            lot_id=str(p.lot_id),
            saga_id=str(p.saga_id) if p.saga_id else None,
            amount_minor=p.amount,
            amount=MoneyOut(**Money(p.amount).to_dict()),
            currency=p.currency,
            status=p.status.value,
            method=p.method.value if p.method else None,
            provider=p.provider.value,
            paid_at=p.paid_at,
            paid_until=p.paid_until,
            operator_id=str(p.operator_id) if p.operator_id else None,
            shift_id=str(p.shift_id) if p.shift_id else None,
            cancel_reason=p.cancel_reason,
            calculation_snapshot=p.calculation_snapshot,
        )


class CashRequest(BaseModel):
    amount_minor: int | None = Field(default=None, ge=0, description="Сверка с суммой, посчитанной на бэкенде")
    method: Literal["CASH", "QR"] = Field(
        default="CASH", description="QR — водитель оплатил по QR площадки, кассир видит поступление (не в кассу смены)"
    )
    reason: str | None = Field(
        default=None, min_length=3, max_length=500, description="Обязателен при нестандартной сумме"
    )


class QuoteOut(BaseModel):
    session_id: str
    amount_minor: int
    amount: MoneyOut
    currency: str = "UZS"


class CheckoutRequest(BaseModel):
    provider: Provider
    return_url: str | None = Field(default=None, max_length=500)


class CheckoutOut(BaseModel):
    payment_id: str
    provider: str
    attempt_id: str
    status: str
    checkout_url: str | None
    provider_transaction_id: str | None

    @classmethod
    def of(cls, c: Checkout) -> CheckoutOut:
        return cls(
            payment_id=str(c.payment_id),
            provider=c.provider.value,
            attempt_id=str(c.attempt_id),
            status=c.status.value,
            checkout_url=c.checkout_url,
            provider_transaction_id=c.provider_transaction_id,
        )


class RefundRequestModel(BaseModel):
    amount_minor: int = Field(gt=0)
    reason: Reason


class RefundOut(BaseModel):
    id: str
    payment_id: str
    amount_minor: int
    amount: MoneyOut
    status: str
    method: str
    reason: str
    shift_id: str | None
    provider_refund_id: str | None
    created_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def of(cls, r: Refund) -> RefundOut:
        return cls(
            id=str(r.id),
            payment_id=str(r.payment_id),
            amount_minor=r.amount,
            amount=MoneyOut(**Money(r.amount).to_dict()),
            status=r.status.value,
            method=r.method.value,
            reason=r.reason,
            shift_id=str(r.shift_id) if r.shift_id else None,
            provider_refund_id=r.provider_refund_id,
            created_at=r.created_at,
            completed_at=r.completed_at,
        )


class ReconciliationRequest(BaseModel):
    provider: Provider
    period_start: date
    period_end: date


class ReconciliationOut(BaseModel):
    id: str
    provider: str
    period_start: date
    period_end: date
    local_count: int
    remote_count: int
    matched_count: int
    discrepancy_count: int
    report: dict[str, Any]

    @classmethod
    def of(cls, r: ReconciliationRun) -> ReconciliationOut:
        return cls(
            id=str(r.id),
            provider=r.provider.value,
            period_start=r.period_start,
            period_end=r.period_end,
            local_count=r.local_count,
            remote_count=r.remote_count,
            matched_count=r.matched_count,
            discrepancy_count=r.discrepancy_count,
            report=r.report,
        )


class DiscrepancyOut(BaseModel):
    id: str
    reconciliation_id: str
    kind: str
    status: str
    payment_id: str | None
    provider_transaction_id: str | None
    local_amount: int | None
    remote_amount: int | None
    details: dict[str, Any]
    resolution: str | None

    @classmethod
    def of(cls, r: DiscrepancyRecord) -> DiscrepancyOut:
        d = r.discrepancy
        return cls(
            id=str(r.id),
            reconciliation_id=str(r.reconciliation_id),
            kind=d.kind.value,
            status=r.status.value,
            payment_id=str(d.payment_id) if d.payment_id else None,
            provider_transaction_id=d.provider_transaction_id,
            local_amount=d.local_amount,
            remote_amount=d.remote_amount,
            details=d.details,
            resolution=r.resolution,
        )


class ResolveRequest(BaseModel):
    status: DiscrepancyStatus
    resolution: Reason


# ---------- смены ----------


@router.post(
    "/shifts",
    response_model=ShiftOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.SHIFT_MANAGE),
)
async def open_shift(
    body: OpenShiftRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SHIFT_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> ShiftOut:
    return ShiftOut.of(
        await container.cashier.open_shift(user, body.lot_id, opening_float=body.opening_float, notes=body.notes)
    )


@router.get("/shifts", response_model=list[ShiftOut], openapi_extra=permission_extra(Permission.SHIFT_MANAGE))
async def list_shifts(
    user: Annotated[CurrentUser, Depends(require(Permission.SHIFT_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> list[ShiftOut]:
    return [ShiftOut.of(s) for s in await container.cashier.list_shifts(user)]


@router.get("/shifts/current", response_model=ShiftOut, openapi_extra=permission_extra(Permission.SHIFT_MANAGE))
async def current_shift(
    user: Annotated[CurrentUser, Depends(require(Permission.SHIFT_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> ShiftOut:
    shift = await container.cashier.current_shift(user)
    if shift is None:
        raise NotFoundError("no open shift")
    return ShiftOut.of(shift)


@router.post(
    "/shifts/{shift_id}/close", response_model=ShiftOut, openapi_extra=permission_extra(Permission.SHIFT_MANAGE)
)
async def close_shift(
    shift_id: UUID,
    body: CloseShiftRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SHIFT_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> ShiftOut:
    return ShiftOut.of(
        await container.cashier.close_shift(user, shift_id, counted_cash=body.counted_cash, notes=body.notes)
    )


# ---------- платежи ----------


@router.get(
    "/sessions/{session_id}/payments",
    response_model=list[PaymentOut],
    openapi_extra=permission_extra(Permission.PAYMENT_READ),
)
async def session_payments(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[PaymentOut]:
    await container.parking.get_session(user, session_id)  # 404 для чужой сессии / вне скоупа
    return [PaymentOut.of(p) for p in await container.cashier.session_payments(user, session_id)]


@router.get(
    "/sessions/{session_id}/quote", response_model=QuoteOut, openapi_extra=permission_extra(Permission.PAYMENT_READ)
)
async def session_quote(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> QuoteOut:
    """Сколько должна сессия сейчас (предоплата у кассира): сумма только с бэкенда."""
    amount = await container.cashier.quote(user, session_id)
    return QuoteOut(session_id=str(session_id), amount_minor=amount, amount=MoneyOut(**Money(amount).to_dict()))


@router.post(
    "/sessions/{session_id}/payments",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PAYMENT_CREATE_CASH),
)
async def prepay_cash(
    session_id: UUID,
    body: CashRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_CREATE_CASH))],
    container: Annotated[Container, Depends(get_container)],
) -> PaymentOut:
    """Предоплата наличными до выезда (A-11): PAID сразу, окно выезда lot.exit_grace_minutes."""
    return PaymentOut.of(
        await container.cashier.prepay(
            user,
            session_id,
            declared_amount=body.amount_minor,
            reason=body.reason,
            method=PaymentMethod(body.method),
        )
    )


@router.get(
    "/payments/{payment_id}", response_model=PaymentOut, openapi_extra=permission_extra(Permission.PAYMENT_READ)
)
async def get_payment(
    payment_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> PaymentOut:
    return PaymentOut.of(await container.cashier.get_payment(user, payment_id))


@router.post(
    "/payments/{payment_id}/confirm-cash",
    response_model=PaymentOut,
    openapi_extra=permission_extra(Permission.PAYMENT_CREATE_CASH),
)
async def confirm_cash(
    payment_id: UUID,
    body: CashRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_CREATE_CASH))],
    container: Annotated[Container, Depends(get_container)],
) -> PaymentOut:
    """PENDING-обязательство саги оплачено наличными в открытой смене кассира; повтор → тот же платёж."""
    return PaymentOut.of(
        await container.cashier.confirm_cash(
            user, payment_id, declared_amount=body.amount_minor, reason=body.reason, method=PaymentMethod(body.method)
        )
    )


class ReceiptOut(BaseModel):
    printed: bool
    receipt_no: str
    amount: MoneyOut


@router.post(
    "/payments/{payment_id}/receipt",
    response_model=ReceiptOut,
    openapi_extra=permission_extra(Permission.PAYMENT_READ),
)
async def print_receipt(
    payment_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> ReceiptOut:
    """Бумажный чек оплаченного платежа на принтер кассы (ESC/POS по сети): после приёма наличных и повторно.
    503 PRINTER_NOT_CONFIGURED / PRINTER_UNAVAILABLE — оплата от этого не зависит."""
    receipt = await container.receipts.print_payment(user, payment_id)
    return ReceiptOut(printed=True, receipt_no=receipt.receipt_no, amount=MoneyOut(**receipt.amount.to_dict()))


@router.post(
    "/sessions/{session_id}/bill",
    response_model=ReceiptOut,
    openapi_extra=permission_extra(Permission.PAYMENT_READ),
)
async def print_bill(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> ReceiptOut:
    """«Счёт к оплате» до оплаты (машина у выезда): номер машины крупно, въезд, сколько стоит, сумма. Номера чека нет —
    он выдаётся чеку оплаты. 422 NOTHING_TO_PAY — платить нечего (сессия закрыта)."""
    bill = await container.receipts.print_bill(user, session_id)
    return ReceiptOut(printed=True, receipt_no="", amount=MoneyOut(**bill.amount.to_dict()))


@router.post(
    "/payments/{payment_id}/checkout",
    response_model=CheckoutOut,
    openapi_extra=permission_extra(Permission.PAYMENT_READ),
)
async def checkout(
    payment_id: UUID,
    body: CheckoutRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> CheckoutOut:
    """Онлайн-оплата: попытка пишется до вызова провайдера, вызов вне транзакции; повтор не создаёт второй intent."""
    return CheckoutOut.of(
        await container.online_payments.checkout(user, payment_id, body.provider, return_url=body.return_url)
    )


@router.post(
    "/payments/{payment_id}/refunds",
    response_model=RefundOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PAYMENT_REFUND),
)
async def create_refund(
    payment_id: UUID,
    body: RefundRequestModel,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_REFUND))],
    container: Annotated[Container, Depends(get_container)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> RefundOut:
    refund = await container.refunds.request(
        user, payment_id, amount=body.amount_minor, reason=body.reason, idempotency_key=idempotency_key
    )
    return RefundOut.of(refund)


@router.get(
    "/payments/{payment_id}/refunds",
    response_model=list[RefundOut],
    openapi_extra=permission_extra(Permission.PAYMENT_READ),
)
async def list_refunds(
    payment_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PAYMENT_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[RefundOut]:
    return [RefundOut.of(r) for r in await container.refunds.list_for_payment(user, payment_id)]


# ---------- вебхуки провайдеров ----------


@router.post("/payments/webhook/{provider}", openapi_extra=PUBLIC, response_model=None)
async def provider_webhook(
    provider: str,
    request: Request,
    container: Annotated[Container, Depends(get_container)],
) -> Response:
    """Payme Merchant API (JSON-RPC, ?merchant=<id>) и Click SHOP API (Prepare/Complete). Подпись → окно времени →
    inbox (дедуп) → применение → ответ по протоколу провайдера."""
    try:
        provider_enum = Provider(provider.upper())
    except ValueError as exc:
        raise NotFoundError("unknown provider") from exc
    if provider_enum is Provider.NONE:
        raise NotFoundError("unknown provider")
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    status_code, payload = await container.webhooks.receive(provider_enum, headers, body, dict(request.query_params))
    return Response(content=json.dumps(payload, default=str), status_code=status_code, media_type="application/json")


# ---------- сверка ----------


@router.post(
    "/reconciliations",
    response_model=ReconciliationOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.RECONCILIATION_MANAGE),
)
async def run_reconciliation(
    body: ReconciliationRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.RECONCILIATION_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> ReconciliationOut:
    run = await container.reconciliation.run(
        user.tenant_id, body.provider, body.period_start, body.period_end, created_by=user.user_id
    )
    return ReconciliationOut.of(run)


@router.get(
    "/reconciliations/{run_id}",
    response_model=ReconciliationOut,
    openapi_extra=permission_extra(Permission.RECONCILIATION_MANAGE),
)
async def get_reconciliation(
    run_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.RECONCILIATION_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> ReconciliationOut:
    return ReconciliationOut.of(await container.reconciliation.get_run(user, run_id))


@router.get(
    "/reconciliation-discrepancies",
    response_model=list[DiscrepancyOut],
    openapi_extra=permission_extra(Permission.RECONCILIATION_MANAGE),
)
async def list_discrepancies(
    user: Annotated[CurrentUser, Depends(require(Permission.RECONCILIATION_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
    status_filter: Annotated[DiscrepancyStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DiscrepancyOut]:
    return [
        DiscrepancyOut.of(d)
        for d in await container.reconciliation.discrepancies(user, status=status_filter, limit=limit)
    ]


@router.post(
    "/reconciliation-discrepancies/{discrepancy_id}/resolve",
    response_model=DiscrepancyOut,
    openapi_extra=permission_extra(Permission.RECONCILIATION_MANAGE),
)
async def resolve_discrepancy(
    discrepancy_id: UUID,
    body: ResolveRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.RECONCILIATION_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> DiscrepancyOut:
    return DiscrepancyOut.of(
        await container.reconciliation.resolve(user, discrepancy_id, status=body.status, resolution=body.resolution)
    )
