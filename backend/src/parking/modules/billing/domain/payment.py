"""Платёж, попытка, возврат, смена — модель без IO. Терминальный платёж не мутируется (I-6): любая коррекция —
компенсирующая запись (Refund) со ссылкой на платёж; статус REFUNDED — производный (триггер БД)."""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from parking.shared.errors import ConflictError, ValidationError


class PaymentStatus(enum.StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"


class PaymentMethod(enum.StrEnum):
    CASH = "CASH"
    CARD = "CARD"
    ONLINE = "ONLINE"
    SUBSCRIPTION = "SUBSCRIPTION"
    VIP = "VIP"
    WHITELIST = "WHITELIST"
    GRACE = "GRACE"


class Provider(enum.StrEnum):
    NONE = "NONE"
    PAYME = "PAYME"
    CLICK = "CLICK"


class AttemptStatus(enum.StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"  # create() не ответил, status() не знает — ретрай create запрещён (§1 п.4)


class RefundStatus(enum.StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


class ShiftStatus(enum.StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


TERMINAL = frozenset({PaymentStatus.PAID, PaymentStatus.CANCELLED, PaymentStatus.REFUNDED, PaymentStatus.FAILED})
PROVIDER_METHODS = frozenset({PaymentMethod.CARD, PaymentMethod.ONLINE})


@dataclass(slots=True, kw_only=True)
class Payment:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    session_id: UUID
    amount: int  # minor units, UZS
    status: PaymentStatus
    idempotency_key: bytes
    calculation_snapshot: dict[str, Any]
    saga_id: UUID | None = None
    method: PaymentMethod | None = None
    provider: Provider = Provider.NONE
    operator_id: UUID | None = None
    shift_id: UUID | None = None
    source_id: UUID | None = None
    paid_at: datetime | None = None
    paid_until: datetime | None = None
    cancel_reason: str | None = None
    created_at: datetime | None = None
    currency: str = "UZS"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def confirm_cash(self, *, operator_id: UUID, shift_id: UUID, now: datetime, paid_until: datetime | None) -> None:
        """PENDING → PAID наличными: только через кассира с открытой сменой (ck_cash_has_operator_shift)."""
        self._require_pending()
        self.method = PaymentMethod.CASH
        self.operator_id = operator_id
        self.shift_id = shift_id
        self.paid_at = now
        self.paid_until = paid_until
        self.status = PaymentStatus.PAID

    def confirm_provider(self, *, provider: Provider, method: PaymentMethod, now: datetime) -> None:
        self._require_pending()
        if method not in PROVIDER_METHODS or provider is Provider.NONE:
            raise ValidationError("provider payment requires CARD/ONLINE and a provider")
        self.method = method
        self.provider = provider
        self.paid_at = now
        self.status = PaymentStatus.PAID

    def fail(self, reason: str, *, provider: Provider = Provider.NONE) -> None:
        """PENDING → FAILED (провайдер отклонил): способ и провайдер фиксируются — ck_method_known_when_settled."""
        self._require_pending()
        if provider is not Provider.NONE:
            self.method = PaymentMethod.ONLINE
            self.provider = provider
        elif self.method is None:
            self.method = PaymentMethod.CASH  # без провайдера отказ возможен только у кассы (ck_provider_methods)
        self.status = PaymentStatus.FAILED
        self.cancel_reason = reason[:60]

    def _require_pending(self) -> None:
        if self.status is not PaymentStatus.PENDING:
            raise ConflictError(f"payment is {self.status.value}, not PENDING")  # I-6: терминальный не трогаем


@dataclass(slots=True, kw_only=True)
class PaymentAttempt:
    """Что было отправлено провайдеру и что он ответил — без секретов и реквизитов; спасает при споре через полгода."""

    id: UUID
    tenant_id: UUID
    payment_id: UUID
    attempt_no: int
    method: PaymentMethod
    provider: Provider
    request_id: UUID
    status: AttemptStatus = AttemptStatus.PENDING
    idempotency_key: bytes | None = None
    provider_transaction_id: str | None = None
    request_payload: dict[str, Any] = field(default_factory=dict)
    response_payload: dict[str, Any] | None = None
    http_status: int | None = None
    provider_error_code: str | None = None
    latency_ms: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    shift_id: UUID | None = None
    operator_id: UUID | None = None


@dataclass(slots=True, kw_only=True)
class Refund:
    id: UUID
    tenant_id: UUID
    payment_id: UUID
    amount: int
    method: PaymentMethod
    operator_id: UUID | None
    reason: str
    idempotency_key: bytes
    status: RefundStatus = RefundStatus.PENDING
    shift_id: UUID | None = None
    provider_refund_id: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    def complete(self, *, now: datetime, provider_refund_id: str | None = None) -> None:
        if self.status is not RefundStatus.PENDING:
            raise ConflictError(f"refund is {self.status.value}")
        self.status = RefundStatus.DONE
        self.completed_at = now
        self.provider_refund_id = provider_refund_id

    def fail(self) -> None:
        if self.status is not RefundStatus.PENDING:
            raise ConflictError(f"refund is {self.status.value}")
        self.status = RefundStatus.FAILED


def refundable_amount(payment_amount: int, existing_refunds: list[int]) -> int:
    """Сколько ещё можно вернуть: сумма платежа минус все незавершившиеся отказом возвраты."""
    return payment_amount - sum(existing_refunds)


def check_refund(payment: Payment, amount: int, existing_refunds: list[int]) -> None:
    """Проверка в коде — быстрый отказ; истина — триггер app.refund_guard под блокировкой платежа."""
    if payment.status not in {PaymentStatus.PAID, PaymentStatus.REFUNDED}:
        raise ConflictError("refund is possible only for PAID payment")
    if amount <= 0:
        raise ValidationError("refund amount must be positive")
    if amount > refundable_amount(payment.amount, existing_refunds):
        raise ValidationError(
            "refund exceeds refundable amount",
            details={"refundable": refundable_amount(payment.amount, existing_refunds)},
        )


@dataclass(slots=True, kw_only=True)
class CashierShift:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    cashier_id: UUID
    status: ShiftStatus = ShiftStatus.OPEN
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    opening_float: int = 0
    expected_cash: int | None = None
    counted_cash: int | None = None
    collected_by: UUID | None = None
    notes: str | None = None

    @property
    def discrepancy(self) -> int | None:
        if self.expected_cash is None or self.counted_cash is None:
            return None
        return self.counted_cash - self.expected_cash

    def close(
        self, *, now: datetime, cash_in: int, cash_out: int, counted_cash: int, by: UUID, notes: str | None
    ) -> None:
        """expected = начальный остаток + наличные платежи смены − наличные возвраты смены; расхождение факт/расчёт
        хранится, не «исправляется»: инкассация сверяет его, а не подгоняет."""
        if self.status is not ShiftStatus.OPEN:
            raise ConflictError("shift is already closed")
        if counted_cash < 0:
            raise ValidationError("counted cash cannot be negative")
        self.expected_cash = self.opening_float + cash_in - cash_out
        self.counted_cash = counted_cash
        self.collected_by = by
        self.notes = notes
        self.closed_at = now
        self.status = ShiftStatus.CLOSED


def payment_key(*parts: object) -> bytes:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()


def amount_matches(expected: int, declared: int | None) -> bool:
    """Сумма считается на бэкенде (§1 п.5); присланная клиентом — только для сверки."""
    return declared is None or declared == expected
