from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.billing.application.ports import (
    DiscrepancyRecord,
    InboxRecord,
    ProviderAccount,
    ReconciliationRun,
    WebhookStatus,
)
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
from parking.modules.billing.domain.reconciliation import Discrepancy, DiscrepancyKind, DiscrepancyStatus

_PAYMENT_COLUMNS = """
    id, tenant_id, lot_id, session_id, saga_id, amount, currency, status::text AS status, method::text AS method,
    provider::text AS provider, operator_id, shift_id, source_id, calculation_snapshot, idempotency_key, paid_at, paid_until,
    cancel_reason, created_at
"""
_ATTEMPT_COLUMNS = """
    id, tenant_id, payment_id, attempt_no, method::text AS method, provider::text AS provider, provider_transaction_id, request_id,
    idempotency_key, request_payload, response_payload, http_status, provider_error_code, status::text AS status, latency_ms,
    started_at, finished_at, shift_id, operator_id
"""
_REFUND_COLUMNS = """
    id, tenant_id, payment_id, amount, status::text AS status, method::text AS method, shift_id, operator_id, reason,
    provider_refund_id, idempotency_key, created_at, completed_at
"""
_SHIFT_COLUMNS = """
    id, tenant_id, lot_id, cashier_id, status::text AS status, opened_at, closed_at, opening_float, expected_cash, counted_cash,
    collected_by, notes
"""
_INBOX_COLUMNS = """
    id, tenant_id, provider::text AS provider, provider_event_id, provider_transaction_id, payload, signature_valid,
    status::text AS status, applied_payment_id, error, response, attempts
"""
_DISC_COLUMNS = """
    id, tenant_id, reconciliation_id, kind::text AS kind, status::text AS status, payment_id, provider_transaction_id,
    local_amount, remote_amount, details, assigned_to, resolution, resolved_by, resolved_at
"""


def _json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return dict(value) if isinstance(value, dict) else dict(json.loads(value))


def _payment(r: Any) -> Payment:
    return Payment(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        session_id=r.session_id,
        saga_id=r.saga_id,
        amount=int(r.amount),
        currency=r.currency,
        status=PaymentStatus(r.status),
        method=PaymentMethod(r.method) if r.method else None,
        provider=Provider(r.provider),
        operator_id=r.operator_id,
        shift_id=r.shift_id,
        source_id=r.source_id,
        calculation_snapshot=_json(r.calculation_snapshot),
        idempotency_key=bytes(r.idempotency_key),
        paid_at=r.paid_at,
        paid_until=r.paid_until,
        cancel_reason=r.cancel_reason,
        created_at=r.created_at,
    )


def _attempt(r: Any) -> PaymentAttempt:
    return PaymentAttempt(
        id=r.id,
        tenant_id=r.tenant_id,
        payment_id=r.payment_id,
        attempt_no=r.attempt_no,
        method=PaymentMethod(r.method),
        provider=Provider(r.provider),
        request_id=r.request_id,
        status=AttemptStatus(r.status),
        idempotency_key=bytes(r.idempotency_key) if r.idempotency_key is not None else None,
        provider_transaction_id=r.provider_transaction_id,
        request_payload=_json(r.request_payload),
        response_payload=_json(r.response_payload) if r.response_payload is not None else None,
        http_status=r.http_status,
        provider_error_code=r.provider_error_code,
        latency_ms=r.latency_ms,
        started_at=r.started_at,
        finished_at=r.finished_at,
        shift_id=r.shift_id,
        operator_id=r.operator_id,
    )


def _refund(r: Any) -> Refund:
    return Refund(
        id=r.id,
        tenant_id=r.tenant_id,
        payment_id=r.payment_id,
        amount=int(r.amount),
        status=RefundStatus(r.status),
        method=PaymentMethod(r.method),
        shift_id=r.shift_id,
        operator_id=r.operator_id,
        reason=r.reason,
        provider_refund_id=r.provider_refund_id,
        idempotency_key=bytes(r.idempotency_key),
        created_at=r.created_at,
        completed_at=r.completed_at,
    )


def _shift(r: Any) -> CashierShift:
    return CashierShift(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        cashier_id=r.cashier_id,
        status=ShiftStatus(r.status),
        opened_at=r.opened_at,
        closed_at=r.closed_at,
        opening_float=int(r.opening_float),
        expected_cash=int(r.expected_cash) if r.expected_cash is not None else None,
        counted_cash=int(r.counted_cash) if r.counted_cash is not None else None,
        collected_by=r.collected_by,
        notes=r.notes,
    )


def _inbox(r: Any) -> InboxRecord:
    return InboxRecord(
        id=r.id,
        tenant_id=r.tenant_id,
        provider=Provider(r.provider),
        provider_event_id=r.provider_event_id,
        provider_transaction_id=r.provider_transaction_id,
        payload=_json(r.payload),
        signature_valid=r.signature_valid,
        status=r.status,
        applied_payment_id=r.applied_payment_id,
        error=r.error,
        response=_json(r.response) if r.response is not None else None,
        attempts=r.attempts,
    )


def _discrepancy(r: Any) -> DiscrepancyRecord:
    return DiscrepancyRecord(
        id=r.id,
        tenant_id=r.tenant_id,
        reconciliation_id=r.reconciliation_id,
        discrepancy=Discrepancy(
            kind=DiscrepancyKind(r.kind),
            payment_id=r.payment_id,
            provider_transaction_id=r.provider_transaction_id,
            local_amount=int(r.local_amount) if r.local_amount is not None else None,
            remote_amount=int(r.remote_amount) if r.remote_amount is not None else None,
            details=_json(r.details),
        ),
        status=DiscrepancyStatus(r.status),
        assigned_to=r.assigned_to,
        resolution=r.resolution,
        resolved_by=r.resolved_by,
        resolved_at=r.resolved_at,
    )


# реестры Payme/Click ведутся по календарным суткам Узбекистана: период сверки — в этой зоне, а не в TimeZone
# сессии БД (иначе результат зависит от настроек сервера и времени суток запуска)
REGISTRY_TZ = "Asia/Tashkent"


class SqlPaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, payment_id: UUID, *, for_update: bool = False) -> Payment | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_PAYMENT_COLUMNS} FROM payment WHERE id = :id{lock}"), {"id": payment_id}
            )
        ).first()
        return None if row is None else _payment(row)

    async def find_by_key(self, idempotency_key: bytes) -> Payment | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_PAYMENT_COLUMNS} FROM payment WHERE idempotency_key = :k"), {"k": idempotency_key}
            )
        ).first()
        return None if row is None else _payment(row)

    async def list_for_session(self, session_id: UUID) -> list[Payment]:
        rows = await self._s.execute(
            text(f"SELECT {_PAYMENT_COLUMNS} FROM payment WHERE session_id = :s ORDER BY created_at"), {"s": session_id}
        )
        return [_payment(r) for r in rows]

    async def pending_for_session(self, session_id: UUID, *, for_update: bool = False) -> Payment | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_PAYMENT_COLUMNS} FROM payment WHERE session_id = :s AND status = 'PENDING' ORDER BY created_at DESC LIMIT 1{lock}"
                ),
                {"s": session_id},
            )
        ).first()
        return None if row is None else _payment(row)

    async def add(self, p: Payment) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO payment (id, tenant_id, lot_id, session_id, saga_id, amount, currency, status, method, provider, operator_id,
                                     shift_id, source_id, calculation_snapshot, idempotency_key, paid_at, paid_until, cancel_reason)
                VALUES (:id, :tenant, :lot, :session, :saga, :amount, :currency, CAST(:status AS payment_status),
                        CAST(:method AS payment_method), CAST(:provider AS payment_provider), :operator, :shift, :source,
                        CAST(:snapshot AS jsonb), :key, :paid_at, :paid_until, :cancel_reason)
                """
            ),
            {
                "id": p.id,
                "tenant": p.tenant_id,
                "lot": p.lot_id,
                "session": p.session_id,
                "saga": p.saga_id,
                "amount": p.amount,
                "currency": p.currency,
                "status": p.status.value,
                "method": p.method.value if p.method else None,
                "provider": p.provider.value,
                "operator": p.operator_id,
                "shift": p.shift_id,
                "source": p.source_id,
                "snapshot": json.dumps(p.calculation_snapshot),
                "key": p.idempotency_key,
                "paid_at": p.paid_at,
                "paid_until": p.paid_until,
                "cancel_reason": p.cancel_reason,
            },
        )

    async def save(self, p: Payment) -> None:
        await self._s.execute(
            text(
                """
                UPDATE payment SET status = CAST(:status AS payment_status), method = CAST(:method AS payment_method),
                    provider = CAST(:provider AS payment_provider), operator_id = :operator, shift_id = :shift, paid_at = :paid_at,
                    paid_until = :paid_until, cancel_reason = :cancel_reason
                WHERE id = :id
                """
            ),
            {
                "id": p.id,
                "status": p.status.value,
                "method": p.method.value if p.method else None,
                "provider": p.provider.value,
                "operator": p.operator_id,
                "shift": p.shift_id,
                "paid_at": p.paid_at,
                "paid_until": p.paid_until,
                "cancel_reason": p.cancel_reason,
            },
        )

    async def settled_for_provider(
        self, provider: Provider, start: date, end: date
    ) -> list[tuple[Payment, str | None]]:
        rows = await self._s.execute(
            text(
                f"""
                SELECT {_PAYMENT_COLUMNS},
                       (SELECT a.provider_transaction_id FROM payment_attempt a
                         WHERE a.payment_id = payment.id AND a.provider_transaction_id IS NOT NULL
                         ORDER BY a.attempt_no DESC LIMIT 1) AS provider_tx
                FROM payment
                WHERE provider = CAST(:provider AS payment_provider) AND status IN ('PAID', 'REFUNDED')
                  AND (paid_at AT TIME ZONE :tz)::date BETWEEN CAST(:start AS date) AND CAST(:end AS date)
                ORDER BY paid_at
                """
            ),
            {"provider": provider.value, "start": start, "end": end, "tz": REGISTRY_TZ},
        )
        return [(_payment(r), r.provider_tx) for r in rows]

    async def session_lot(self, session_id: UUID) -> UUID | None:
        row = (
            await self._s.execute(text("SELECT lot_id FROM parking_session WHERE id = :s"), {"s": session_id})
        ).first()
        return None if row is None else row.lot_id


class SqlAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, a: PaymentAttempt) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO payment_attempt (id, tenant_id, payment_id, attempt_no, method, provider, provider_transaction_id, request_id,
                                             idempotency_key, request_payload, status, shift_id, operator_id, started_at)
                VALUES (:id, :tenant, :payment, :no, CAST(:method AS payment_method), CAST(:provider AS payment_provider), :tx, :request_id,
                        :key, CAST(:request AS jsonb), CAST(:status AS attempt_status), :shift, :operator, coalesce(:started, now()))
                """
            ),
            {
                "id": a.id,
                "tenant": a.tenant_id,
                "payment": a.payment_id,
                "no": a.attempt_no,
                "method": a.method.value,
                "provider": a.provider.value,
                "tx": a.provider_transaction_id,
                "request_id": a.request_id,
                "key": a.idempotency_key,
                "request": json.dumps(a.request_payload, default=str),
                "status": a.status.value,
                "shift": a.shift_id,
                "operator": a.operator_id,
                "started": a.started_at,
            },
        )

    async def save(self, a: PaymentAttempt) -> None:
        await self._s.execute(
            text(
                """
                UPDATE payment_attempt SET provider_transaction_id = :tx, response_payload = CAST(:response AS jsonb), http_status = :http,
                    provider_error_code = :err, status = CAST(:status AS attempt_status), latency_ms = :latency, finished_at = :finished
                WHERE id = :id
                """
            ),
            {
                "id": a.id,
                "tx": a.provider_transaction_id,
                "response": json.dumps(a.response_payload, default=str) if a.response_payload is not None else None,
                "http": a.http_status,
                "err": a.provider_error_code,
                "status": a.status.value,
                "latency": a.latency_ms,
                "finished": a.finished_at,
            },
        )

    async def get(self, attempt_id: UUID, *, for_update: bool = False) -> PaymentAttempt | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempt WHERE id = :id{lock}"), {"id": attempt_id}
            )
        ).first()
        return None if row is None else _attempt(row)

    async def next_no(self, payment_id: UUID) -> int:
        row = (
            await self._s.execute(
                text("SELECT coalesce(max(attempt_no), 0) + 1 AS n FROM payment_attempt WHERE payment_id = :p"),
                {"p": payment_id},
            )
        ).first()
        return int(row.n) if row else 1

    async def latest(self, payment_id: UUID) -> PaymentAttempt | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempt WHERE payment_id = :p ORDER BY attempt_no DESC LIMIT 1"
                ),
                {"p": payment_id},
            )
        ).first()
        return None if row is None else _attempt(row)

    async def find_by_provider_tx(self, provider: Provider, provider_transaction_id: str) -> PaymentAttempt | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempt WHERE provider = CAST(:provider AS payment_provider) AND provider_transaction_id = :tx ORDER BY started_at DESC LIMIT 1"
                ),
                {"provider": provider.value, "tx": provider_transaction_id},
            )
        ).first()
        return None if row is None else _attempt(row)

    async def unknown(self, *, limit: int) -> list[PaymentAttempt]:
        rows = await self._s.execute(
            text(
                f"SELECT {_ATTEMPT_COLUMNS} FROM payment_attempt WHERE status IN ('UNKNOWN', 'TIMEOUT') ORDER BY started_at LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [_attempt(r) for r in rows]

    async def list_between(self, provider: Provider, start: datetime, end: datetime) -> list[PaymentAttempt]:
        rows = await self._s.execute(
            text(
                f"""
                SELECT {_ATTEMPT_COLUMNS} FROM payment_attempt
                WHERE provider = CAST(:provider AS payment_provider) AND provider_transaction_id IS NOT NULL
                  AND started_at >= :start AND started_at < :end ORDER BY started_at
                """
            ),
            {"provider": provider.value, "start": start, "end": end},
        )
        return [_attempt(r) for r in rows]


class SqlRefundRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, r: Refund) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO refund (id, tenant_id, payment_id, amount, status, method, shift_id, operator_id, reason, provider_refund_id,
                                    idempotency_key, completed_at)
                VALUES (:id, :tenant, :payment, :amount, CAST(:status AS refund_status), CAST(:method AS payment_method), :shift,
                        :operator, :reason, :provider_refund_id, :key, :completed)
                """
            ),
            {
                "id": r.id,
                "tenant": r.tenant_id,
                "payment": r.payment_id,
                "amount": r.amount,
                "status": r.status.value,
                "method": r.method.value,
                "shift": r.shift_id,
                "operator": r.operator_id,
                "reason": r.reason,
                "provider_refund_id": r.provider_refund_id,
                "key": r.idempotency_key,
                "completed": r.completed_at,
            },
        )

    async def save(self, r: Refund) -> None:
        await self._s.execute(
            text(
                "UPDATE refund SET status = CAST(:status AS refund_status), provider_refund_id = :prid, completed_at = :completed WHERE id = :id"
            ),
            {"id": r.id, "status": r.status.value, "prid": r.provider_refund_id, "completed": r.completed_at},
        )

    async def get(self, refund_id: UUID, *, for_update: bool = False) -> Refund | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(text(f"SELECT {_REFUND_COLUMNS} FROM refund WHERE id = :id{lock}"), {"id": refund_id})
        ).first()
        return None if row is None else _refund(row)

    async def find_by_key(self, idempotency_key: bytes) -> Refund | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_REFUND_COLUMNS} FROM refund WHERE idempotency_key = :k"), {"k": idempotency_key}
            )
        ).first()
        return None if row is None else _refund(row)

    async def list_for_payment(self, payment_id: UUID) -> list[Refund]:
        rows = await self._s.execute(
            text(f"SELECT {_REFUND_COLUMNS} FROM refund WHERE payment_id = :p ORDER BY created_at"), {"p": payment_id}
        )
        return [_refund(r) for r in rows]

    async def pending_provider(self, *, limit: int) -> list[Refund]:
        rows = await self._s.execute(
            text(
                f"SELECT {_REFUND_COLUMNS} FROM refund WHERE status = 'PENDING' AND method IN ('CARD', 'ONLINE') ORDER BY created_at LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [_refund(r) for r in rows]


class SqlShiftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, s: CashierShift) -> None:
        await self._s.execute(
            text(
                "INSERT INTO cashier_shift (id, tenant_id, lot_id, cashier_id, opening_float, notes) VALUES (:id, :tenant, :lot, :cashier, :float, :notes)"
            ),
            {
                "id": s.id,
                "tenant": s.tenant_id,
                "lot": s.lot_id,
                "cashier": s.cashier_id,
                "float": s.opening_float,
                "notes": s.notes,
            },
        )

    async def get(self, shift_id: UUID, *, for_update: bool = False) -> CashierShift | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_SHIFT_COLUMNS} FROM cashier_shift WHERE id = :id{lock}"), {"id": shift_id}
            )
        ).first()
        return None if row is None else _shift(row)

    async def open_for_cashier(self, cashier_id: UUID, *, for_update: bool = False) -> CashierShift | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_SHIFT_COLUMNS} FROM cashier_shift WHERE cashier_id = :c AND status = 'OPEN'{lock}"),
                {"c": cashier_id},
            )
        ).first()
        return None if row is None else _shift(row)

    async def save(self, s: CashierShift) -> None:
        await self._s.execute(
            text(
                """
                UPDATE cashier_shift SET status = CAST(:status AS shift_status), closed_at = :closed, expected_cash = :expected,
                    counted_cash = :counted, collected_by = :by, notes = :notes
                WHERE id = :id
                """
            ),
            {
                "id": s.id,
                "status": s.status.value,
                "closed": s.closed_at,
                "expected": s.expected_cash,
                "counted": s.counted_cash,
                "by": s.collected_by,
                "notes": s.notes,
            },
        )

    async def cash_totals(self, shift_id: UUID) -> tuple[int, int]:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT (SELECT coalesce(sum(amount), 0) FROM payment WHERE shift_id = :s AND method = 'CASH' AND status IN ('PAID', 'REFUNDED')) AS cash_in,
                           (SELECT coalesce(sum(amount), 0) FROM refund WHERE shift_id = :s AND method = 'CASH' AND status = 'DONE') AS cash_out
                    """
                ),
                {"s": shift_id},
            )
        ).first()
        assert row is not None
        return int(row.cash_in), int(row.cash_out)

    async def list(self, *, lot_ids: frozenset[UUID] | None, limit: int) -> list[CashierShift]:
        where = "" if lot_ids is None else " WHERE lot_id = ANY(:lots)"
        params: dict[str, Any] = {"limit": limit}
        if lot_ids is not None:
            params["lots"] = list(lot_ids)
        rows = await self._s.execute(
            text(f"SELECT {_SHIFT_COLUMNS} FROM cashier_shift{where} ORDER BY opened_at DESC LIMIT :limit"), params
        )
        return [_shift(r) for r in rows]


class SqlWebhookInboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, r: InboxRecord) -> InboxRecord | None:
        inserted = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO webhook_inbox (id, tenant_id, provider, provider_event_id, provider_transaction_id, payload, signature_valid, status)
                    VALUES (:id, :tenant, CAST(:provider AS payment_provider), :event_id, :tx, CAST(:payload AS jsonb), :sig, CAST(:status AS webhook_status))
                    ON CONFLICT (provider, provider_event_id) DO NOTHING RETURNING id
                    """
                ),
                {
                    "id": r.id,
                    "tenant": r.tenant_id,
                    "provider": r.provider.value,
                    "event_id": r.provider_event_id,
                    "tx": r.provider_transaction_id,
                    "payload": json.dumps(r.payload, default=str),
                    "sig": r.signature_valid,
                    "status": r.status,
                },
            )
        ).first()
        if inserted is not None:
            return None
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_INBOX_COLUMNS} FROM webhook_inbox WHERE provider = CAST(:provider AS payment_provider) AND provider_event_id = :event_id"
                ),
                {"provider": r.provider.value, "event_id": r.provider_event_id},
            )
        ).first()
        return None if row is None else _inbox(row)

    async def save(self, r: InboxRecord) -> None:
        await self._s.execute(
            text(
                """
                UPDATE webhook_inbox SET status = CAST(:status AS webhook_status), applied_payment_id = :applied, error = :error,
                    response = CAST(:response AS jsonb), attempts = :attempts, provider_transaction_id = :tx,
                    processed_at = CASE WHEN :status IN ('APPLIED', 'REJECTED', 'DUPLICATE') THEN now() ELSE processed_at END
                WHERE id = :id
                """
            ),
            {
                "id": r.id,
                "status": r.status,
                "applied": r.applied_payment_id,
                "error": r.error,
                "response": json.dumps(r.response, default=str) if r.response is not None else None,
                "attempts": r.attempts,
                "tx": r.provider_transaction_id,
            },
        )

    async def orphans(self, *, limit: int) -> list[InboxRecord]:
        rows = await self._s.execute(
            text(
                f"SELECT {_INBOX_COLUMNS} FROM webhook_inbox WHERE status = 'ORPHAN' ORDER BY received_at LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [_inbox(r) for r in rows]


class SqlProviderAccountLookup:
    """Pre-tenant поиск через app.lookup_provider_account (SECURITY DEFINER) и tenant-поиск обычным SELECT."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def by_merchant(self, provider: Provider, merchant_id: str) -> ProviderAccount | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT id, tenant_id, secret_ref, active FROM app.lookup_provider_account(CAST(:provider AS payment_provider), :merchant)"
                ),
                {"provider": provider.value, "merchant": merchant_id},
            )
        ).first()
        if row is None:
            return None
        return ProviderAccount(row.id, row.tenant_id, provider, merchant_id, row.secret_ref, row.active)

    async def for_tenant(self, provider: Provider) -> ProviderAccount | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT id, tenant_id, merchant_id, secret_ref, active FROM provider_account WHERE provider = CAST(:provider AS payment_provider) AND active"
                ),
                {"provider": provider.value},
            )
        ).first()
        if row is None:
            return None
        return ProviderAccount(row.id, row.tenant_id, provider, row.merchant_id, row.secret_ref, row.active)


class SqlReconciliationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_run(self, run: ReconciliationRun) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO payment_reconciliation (id, tenant_id, provider, period_start, period_end, started_at, created_by)
                VALUES (:id, :tenant, CAST(:provider AS payment_provider), :start, :end, :started, :by)
                """
            ),
            {
                "id": run.id,
                "tenant": run.tenant_id,
                "provider": run.provider.value,
                "start": run.period_start,
                "end": run.period_end,
                "started": run.started_at,
                "by": run.created_by,
            },
        )

    async def save_run(self, run: ReconciliationRun) -> None:
        await self._s.execute(
            text(
                """
                UPDATE payment_reconciliation SET finished_at = :finished, local_count = :lc, remote_count = :rc, matched_count = :mc,
                    discrepancy_count = :dc, report = CAST(:report AS jsonb)
                WHERE id = :id
                """
            ),
            {
                "id": run.id,
                "finished": run.finished_at,
                "lc": run.local_count,
                "rc": run.remote_count,
                "mc": run.matched_count,
                "dc": run.discrepancy_count,
                "report": json.dumps(run.report, default=str),
            },
        )

    async def get_run(self, run_id: UUID) -> ReconciliationRun | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT id, tenant_id, provider::text AS provider, period_start, period_end, started_at, finished_at, local_count, remote_count, matched_count, discrepancy_count, report, created_by FROM payment_reconciliation WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).first()
        if row is None:
            return None
        return ReconciliationRun(
            id=row.id,
            tenant_id=row.tenant_id,
            provider=Provider(row.provider),
            period_start=row.period_start,
            period_end=row.period_end,
            started_at=row.started_at,
            finished_at=row.finished_at,
            local_count=row.local_count or 0,
            remote_count=row.remote_count or 0,
            matched_count=row.matched_count or 0,
            discrepancy_count=row.discrepancy_count or 0,
            report=_json(row.report),
            created_by=row.created_by,
        )

    async def add_discrepancy(self, rec: DiscrepancyRecord) -> None:
        d = rec.discrepancy
        await self._s.execute(
            text(
                """
                INSERT INTO reconciliation_discrepancy (id, tenant_id, reconciliation_id, kind, status, payment_id, provider_transaction_id,
                                                        local_amount, remote_amount, details)
                VALUES (:id, :tenant, :run, CAST(:kind AS discrepancy_kind), CAST(:status AS discrepancy_status), :payment, :tx, :la, :ra,
                        CAST(:details AS jsonb))
                """
            ),
            {
                "id": rec.id,
                "tenant": rec.tenant_id,
                "run": rec.reconciliation_id,
                "kind": d.kind.value,
                "status": rec.status.value,
                "payment": d.payment_id,
                "tx": d.provider_transaction_id,
                "la": d.local_amount,
                "ra": d.remote_amount,
                "details": json.dumps(d.details, default=str),
            },
        )

    async def get_discrepancy(self, record_id: UUID, *, for_update: bool = False) -> DiscrepancyRecord | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_DISC_COLUMNS} FROM reconciliation_discrepancy WHERE id = :id{lock}"), {"id": record_id}
            )
        ).first()
        return None if row is None else _discrepancy(row)

    async def save_discrepancy(self, rec: DiscrepancyRecord) -> None:
        await self._s.execute(
            text(
                """
                UPDATE reconciliation_discrepancy SET status = CAST(:status AS discrepancy_status), assigned_to = :assigned,
                    resolution = :resolution, resolved_by = :by, resolved_at = :at
                WHERE id = :id
                """
            ),
            {
                "id": rec.id,
                "status": rec.status.value,
                "assigned": rec.assigned_to,
                "resolution": rec.resolution,
                "by": rec.resolved_by,
                "at": rec.resolved_at,
            },
        )

    async def list_discrepancies(self, *, status: DiscrepancyStatus | None, limit: int) -> list[DiscrepancyRecord]:
        where = "" if status is None else " WHERE status = CAST(:status AS discrepancy_status)"
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status.value
        rows = await self._s.execute(
            text(
                f"SELECT {_DISC_COLUMNS} FROM reconciliation_discrepancy{where} ORDER BY created_at DESC LIMIT :limit"
            ),
            params,
        )
        return [_discrepancy(r) for r in rows]

    async def open_count(self) -> int:
        row = (
            await self._s.execute(text("SELECT count(*) AS n FROM reconciliation_discrepancy WHERE status = 'OPEN'"))
        ).first()
        return int(row.n) if row else 0


__all__ = [
    "SqlAttemptRepository",
    "SqlPaymentRepository",
    "SqlProviderAccountLookup",
    "SqlReconciliationRepository",
    "SqlRefundRepository",
    "SqlShiftRepository",
    "SqlWebhookInboxRepository",
    "WebhookStatus",
]
