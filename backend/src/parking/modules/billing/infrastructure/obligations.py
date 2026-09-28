from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from parking.modules.billing.application.contracts import PaymentRef, PaymentStatus, PrivilegeMethod
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.shared.clock import Clock


def _ref(r: Any) -> PaymentRef:
    return PaymentRef(
        id=r.id,
        status=PaymentStatus(r.status),
        amount_minor=int(r.amount),
        paid_until=r.paid_until,
        verification_pending=bool(getattr(r, "unverified", False)),
        session_id=getattr(r, "session_id", None),
        paid_at=getattr(r, "paid_at", None),
    )


class SqlPaymentObligations:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @property
    def _tenant(self) -> UUID:
        assert self._uow.tenant_id is not None
        return self._uow.tenant_id

    async def _existing(self, idempotency_key: bytes) -> PaymentRef | None:
        row = (
            await self._uow.session.execute(
                text(
                    "SELECT id, status::text AS status, amount, paid_until FROM payment WHERE tenant_id = :t AND idempotency_key = :k"
                ),
                {"t": self._tenant, "k": idempotency_key},
            )
        ).first()
        return None if row is None else _ref(row)

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
        existing = await self._existing(idempotency_key)
        if existing is not None:
            return existing
        payment_id = uuid4()
        await self._uow.session.execute(
            text(
                """
                INSERT INTO payment (id, tenant_id, lot_id, session_id, saga_id, amount, status, method, calculation_snapshot, idempotency_key)
                VALUES (:id, :t, :lot, :session, :saga, :amount, 'PENDING', NULL, CAST(:snapshot AS jsonb), :key)
                """
            ),
            {
                "id": payment_id,
                "t": self._tenant,
                "lot": lot_id,
                "session": session_id,
                "saga": saga_id,
                "amount": amount_minor,
                "snapshot": json.dumps(snapshot),
                "key": idempotency_key,
            },
        )
        self._uow.publish(
            IntegrationEvent(
                type="billing.payment.requested.v1",
                tenant_id=self._tenant,
                aggregate_type="payment",
                aggregate_id=payment_id,
                payload={
                    "payment_id": str(payment_id),
                    "session_id": str(session_id),
                    "lot_id": str(lot_id),
                    "saga_id": str(saga_id),
                    "amount": amount_minor,
                    "currency": "UZS",
                },
            )
        )
        return PaymentRef(id=payment_id, status=PaymentStatus.PENDING, amount_minor=amount_minor)

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
        existing = await self._existing(idempotency_key)
        if existing is not None:
            return existing
        payment_id = uuid4()
        now = self._clock.now()
        await self._uow.session.execute(
            text(
                """
                INSERT INTO payment (id, tenant_id, lot_id, session_id, saga_id, amount, status, method, source_id, calculation_snapshot, idempotency_key, paid_at)
                VALUES (:id, :t, :lot, :session, :saga, 0, 'PAID', CAST(:method AS payment_method), :source, CAST(:snapshot AS jsonb), :key, :paid_at)
                """
            ),
            {
                "id": payment_id,
                "t": self._tenant,
                "lot": lot_id,
                "session": session_id,
                "saga": saga_id,
                "method": method.value,
                "source": source_id,
                "snapshot": json.dumps(snapshot),
                "key": idempotency_key,
                "paid_at": now,
            },
        )
        self._uow.publish(
            IntegrationEvent(
                type="billing.payment.confirmed.v1",
                tenant_id=self._tenant,
                aggregate_type="payment",
                aggregate_id=payment_id,
                payload={
                    "payment_id": str(payment_id),
                    "session_id": str(session_id),
                    "lot_id": str(lot_id),
                    "saga_id": str(saga_id),
                    "amount": 0,
                    "method": method.value,
                    "paid_at": now.isoformat(),
                },
            )
        )
        return PaymentRef(id=payment_id, status=PaymentStatus.PAID, amount_minor=0)

    async def status(self, payment_id: UUID) -> PaymentRef | None:
        row = (
            await self._uow.session.execute(
                text(
                    """
                    SELECT p.id, p.status::text AS status, p.amount, p.paid_until, p.session_id, p.paid_at,
                           EXISTS (SELECT 1 FROM payment_attempt a WHERE a.payment_id = p.id AND a.status IN ('UNKNOWN', 'TIMEOUT')) AS unverified
                    FROM payment p WHERE p.id = :id
                    """
                ),
                {"id": payment_id},
            )
        ).first()
        return None if row is None else _ref(row)

    async def prepaid_expired(self, before: datetime, *, limit: int) -> list[PaymentRef]:
        rows = await self._uow.session.execute(
            text(
                """
                SELECT id, status::text AS status, amount, paid_until, session_id, paid_at FROM payment
                WHERE status = 'PAID' AND paid_until IS NOT NULL AND paid_until < CAST(:before AS timestamptz)
                ORDER BY paid_until LIMIT :limit
                """
            ),
            {"before": before, "limit": limit},
        )
        return [_ref(r) for r in rows]

    async def prepaid_for(self, session_id: UUID, at: datetime) -> PaymentRef | None:
        row = (
            await self._uow.session.execute(
                text(
                    "SELECT id, status::text AS status, amount, paid_until FROM payment WHERE session_id = :s AND status = 'PAID' AND paid_until IS NOT NULL AND paid_until >= CAST(:at AS timestamptz) ORDER BY paid_until DESC LIMIT 1"
                ),
                {"s": session_id, "at": at},
            )
        ).first()
        return None if row is None else _ref(row)

    async def cancel_obligation(self, payment_id: UUID, reason: str) -> bool:
        result = await self._uow.session.execute(
            text(
                "UPDATE payment SET status = 'CANCELLED', cancel_reason = :reason WHERE id = :id AND status = 'PENDING' RETURNING session_id"
            ),
            {"id": payment_id, "reason": reason[:60]},
        )
        row = result.first()
        if row is None:
            return False
        self._uow.publish(
            IntegrationEvent(
                type="billing.payment.cancelled.v1",
                tenant_id=self._tenant,
                aggregate_type="payment",
                aggregate_id=payment_id,
                payload={"payment_id": str(payment_id), "session_id": str(row.session_id), "reason": reason},
            )
        )
        return True
