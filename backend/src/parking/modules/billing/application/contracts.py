"""Контракт Billing: для EXIT-саги — обязательства (фаза 3.5); для API и других модулей — представления
платежей, возвратов, смен, сверки и сервисы фазы 5 (только через этот модуль)."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.billing.application.online import Checkout
from parking.modules.billing.application.ports import DiscrepancyRecord, ReconciliationRun
from parking.modules.billing.domain.payment import (
    AttemptStatus,
    CashierShift,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    Provider,
    Refund,
    RefundStatus,
    ShiftStatus,
)
from parking.modules.billing.domain.reconciliation import DiscrepancyKind, DiscrepancyStatus, RemoteState, RemoteTx

__all__ = [
    "AttemptStatus",
    "CashierShift",
    "Checkout",
    "DiscrepancyKind",
    "DiscrepancyRecord",
    "DiscrepancyStatus",
    "Payment",
    "PaymentAttempt",
    "PaymentMethod",
    "PaymentObligations",
    "PaymentRef",
    "PaymentStatus",
    "PrivilegeMethod",
    "Provider",
    "ReconciliationRun",
    "Refund",
    "RefundStatus",
    "RemoteState",
    "RemoteTx",
    "ShiftStatus",
]


class PrivilegeMethod(enum.StrEnum):
    SUBSCRIPTION = "SUBSCRIPTION"
    VIP = "VIP"
    WHITELIST = "WHITELIST"
    GRACE = "GRACE"


@dataclass(frozen=True, slots=True)
class PaymentRef:
    id: UUID
    status: PaymentStatus
    amount_minor: int
    paid_until: datetime | None = None
    verification_pending: bool = False  # create() провайдера не ответил, status() не знает — сага ждёт, не ретраит
    session_id: UUID | None = None
    paid_at: datetime | None = None


class PaymentObligations(Protocol):
    """Все методы — внутри UoW вызывающего (сага), без внешних вызовов."""

    async def create_obligation(
        self,
        *,
        session_id: UUID,
        lot_id: UUID,
        saga_id: UUID,
        amount_minor: int,
        snapshot: dict[str, Any],
        idempotency_key: bytes,
    ) -> PaymentRef:
        """PENDING-платёж; повтор с тем же ключом возвращает существующий."""
        ...

    async def record_privilege(
        self,
        *,
        session_id: UUID,
        lot_id: UUID,
        saga_id: UUID,
        method: PrivilegeMethod,
        source_id: UUID | None,
        snapshot: dict[str, Any],
        idempotency_key: bytes,
    ) -> PaymentRef:
        """PAID-платёж на 0: льгота видна в отчётах как использование, а не как отсутствие платежа."""
        ...

    async def status(self, payment_id: UUID) -> PaymentRef | None: ...
    async def prepaid_for(self, session_id: UUID, at: datetime) -> PaymentRef | None:
        """PAID-платёж сессии, чей paid_until ≥ at (предоплата, A-11)."""
        ...

    async def cancel_obligation(self, payment_id: UUID, reason: str) -> bool: ...

    async def prepaid_expired(self, before: datetime, *, limit: int) -> list[PaymentRef]:
        """PAID-предоплаты, чей paid_until < before: сессия без EXIT-события закрывается как INFERRED_BY_PAYMENT."""
        ...
