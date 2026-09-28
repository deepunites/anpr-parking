"""Общее для сервисов Billing: набор репозиториев на UoW, событие подтверждения, аудит."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.billing.application.ports import (
    AttemptRepository,
    PaymentRepository,
    ProviderAccountLookup,
    ReconciliationRepository,
    RefundRepository,
    ShiftRepository,
    WebhookInboxRepository,
)
from parking.modules.billing.domain.payment import Payment, Refund
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.tenancy.application.contracts import LotDirectory
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent


@dataclass(frozen=True, slots=True)
class BillingRepositories:
    payments: PaymentRepository
    attempts: AttemptRepository
    refunds: RefundRepository
    shifts: ShiftRepository
    inbox: WebhookInboxRepository
    accounts: ProviderAccountLookup
    reconciliation: ReconciliationRepository
    lots: LotDirectory
    audit: AuditWriter


UowFactory = Callable[[UUID | None], UnitOfWork]
ReposFactory = Callable[[UnitOfWork], BillingRepositories]


def payment_confirmed(payment: Payment) -> IntegrationEvent:
    assert payment.paid_at is not None and payment.method is not None
    return IntegrationEvent(
        type="billing.payment.confirmed.v1",
        tenant_id=payment.tenant_id,
        aggregate_type="payment",
        aggregate_id=payment.id,
        payload={
            "payment_id": str(payment.id),
            "session_id": str(payment.session_id),
            "lot_id": str(payment.lot_id),
            "saga_id": str(payment.saga_id) if payment.saga_id else None,
            "amount": payment.amount,
            "method": payment.method.value,
            "paid_at": payment.paid_at.isoformat(),
        },
    )


def record_paid(payment: Payment) -> None:
    """Бизнес-метрики факта оплаты: выручка (тийины) и латентность от обязательства до PAID."""
    method = payment.method.value if payment.method else "UNKNOWN"
    metrics.revenue_total.labels(method=method).inc(payment.amount)
    if payment.created_at is not None and payment.paid_at is not None:
        metrics.payment_confirm_latency_seconds.labels(method=method).observe(
            max((payment.paid_at - payment.created_at).total_seconds(), 0.0)
        )


def payment_failed(payment: Payment, reason: str) -> IntegrationEvent:
    return IntegrationEvent(
        type="billing.payment.failed.v1",
        tenant_id=payment.tenant_id,
        aggregate_type="payment",
        aggregate_id=payment.id,
        payload={
            "payment_id": str(payment.id),
            "session_id": str(payment.session_id),
            "saga_id": str(payment.saga_id) if payment.saga_id else None,
            "reason": reason,
        },
    )


def refund_event(refund: Refund, kind: str) -> IntegrationEvent:
    return IntegrationEvent(
        type=f"billing.refund.{kind}.v1",
        tenant_id=refund.tenant_id,
        aggregate_type="refund",
        aggregate_id=refund.id,
        payload={
            "refund_id": str(refund.id),
            "payment_id": str(refund.payment_id),
            "amount": refund.amount,
            "method": refund.method.value,
            "status": refund.status.value,
            "reason": refund.reason,
        },
    )


async def audit(
    repos: BillingRepositories,
    *,
    tenant_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    actor: CurrentUser | None,
    lot_id: UUID | None = None,
    session_id: UUID | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    actor_type: ActorType | None = None,
) -> None:
    kind = actor_type or (ActorType.USER if actor else ActorType.SYSTEM)
    if kind is ActorType.USER and not (reason and len(reason.strip()) >= 3):
        reason = action.lower().replace("_", " ")  # ck_audit_user_reason: действие пользователя всегда с основанием
    await repos.audit.record(
        AuditEntry(
            tenant_id=tenant_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            lot_id=lot_id,
            session_id=session_id,
            actor_type=kind,
            actor_id=actor.user_id if actor else None,
            user_id=actor.user_id if actor else None,
            reason=reason,
            payload=dict(payload or {}),
        )
    )


def check_lot_scope(actor: CurrentUser, lot_id: UUID) -> bool:
    allowed = actor.allowed_lots()
    return allowed is None or lot_id in allowed


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
