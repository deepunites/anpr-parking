"""I-6. Payment в статусе PAID/REFUNDED никогда не мутируется.

Механизм: BEFORE UPDATE триггер app.payment_guard (разрешены только PENDING → PAID|FAILED|CANCELLED и PAID → REFUNDED
сменой одного лишь статуса), BEFORE DELETE триггер app.forbid_delete, refund_guard (сумма возвратов ≤ платежа под
блокировкой), производный REFUNDED через refund_settle.
При нарушении: суммы правятся задним числом, аудит платежей теряет смысл.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import asyncpg
import pytest

from parking.platform.db.tenant import tenant_transaction
from tests.conftest import CAM_A1_IN, LOT_A1, SHIFT_A, TENANT_A, USER_A_CASHIER, expect_error, new_session, new_vehicle


async def _paid_payment(owner: asyncpg.Connection, amount: int = 500000) -> UUID:
    vehicle_id = await new_vehicle(owner, TENANT_A, f"I06-{uuid4()}")
    session_id = await new_session(owner, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)
    row = await owner.fetchrow(
        """
        INSERT INTO payment (tenant_id, lot_id, session_id, amount, status, method, operator_id, shift_id, calculation_snapshot,
                             idempotency_key, paid_at)
        VALUES ($1, $2, $3, $4, 'PAID', 'CASH', $5, $6, '{}', $7, now()) RETURNING id
        """,
        TENANT_A,
        LOT_A1,
        session_id,
        amount,
        USER_A_CASHIER,
        SHIFT_A,
        hashlib.sha256(uuid4().bytes).digest(),
    )
    assert row is not None
    return UUID(str(row["id"]))


async def test_pending_to_paid_is_allowed(owner: asyncpg.Connection, critical: asyncpg.Connection) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I06-pending")
    session_id = await new_session(owner, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)
    async with tenant_transaction(critical, TENANT_A):
        payment_id = await critical.fetchval(
            """
            INSERT INTO payment (tenant_id, lot_id, session_id, amount, method, operator_id, shift_id, calculation_snapshot, idempotency_key)
            VALUES ($1, $2, $3, 300000, 'CASH', $4, $5, '{}', $6) RETURNING id
            """,
            TENANT_A,
            LOT_A1,
            session_id,
            USER_A_CASHIER,
            SHIFT_A,
            hashlib.sha256(b"I06-pending").digest(),
        )
        await critical.execute("UPDATE payment SET status = 'PAID', paid_at = now() WHERE id = $1", payment_id)
        assert await critical.fetchval("SELECT status::text FROM payment WHERE id = $1", payment_id) == "PAID"


async def test_paid_payment_amount_update_rejected_under_app_role(
    owner: asyncpg.Connection, critical: asyncpg.Connection
) -> None:
    payment_id = await _paid_payment(owner)
    for stmt in (
        "UPDATE payment SET amount = 1 WHERE id = $1",
        "UPDATE payment SET status = 'CANCELLED' WHERE id = $1",
        "UPDATE payment SET status = 'PENDING' WHERE id = $1",
    ):
        await expect_error(critical, TENANT_A, asyncpg.CheckViolationError, stmt, payment_id, match=r"\[I-6\]")


async def test_paid_payment_is_immutable_even_for_owner(owner: asyncpg.Connection) -> None:
    payment_id = await _paid_payment(owner)
    with pytest.raises(asyncpg.CheckViolationError, match=r"\[I-6\]"):
        await owner.execute("UPDATE payment SET paid_at = now() - interval '1 day' WHERE id = $1", payment_id)
    with pytest.raises(asyncpg.CheckViolationError, match=r"\[I-6\]"):
        await owner.execute("DELETE FROM payment WHERE id = $1", payment_id)


async def test_pending_amount_is_immutable_too(owner: asyncpg.Connection) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I06-pending-amount")
    session_id = await new_session(owner, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)
    payment_id = await owner.fetchval(
        """
        INSERT INTO payment (tenant_id, lot_id, session_id, amount, method, provider, calculation_snapshot, idempotency_key)
        VALUES ($1, $2, $3, 300000, 'ONLINE', 'PAYME', '{}', $4) RETURNING id
        """,
        TENANT_A,
        LOT_A1,
        session_id,
        hashlib.sha256(b"I06-pending-amount").digest(),
    )
    with pytest.raises(asyncpg.CheckViolationError, match=r"\[I-6\]"):
        await owner.execute("UPDATE payment SET amount = 1 WHERE id = $1", payment_id)


async def test_refund_over_amount_rejected_and_full_refund_derives_refunded(owner: asyncpg.Connection) -> None:
    payment_id = await _paid_payment(owner, amount=1000)

    async def refund(amount: int, status: str = "DONE") -> None:
        await owner.execute(
            """
            INSERT INTO refund (tenant_id, payment_id, amount, status, method, shift_id, operator_id, reason, idempotency_key)
            VALUES ($1, $2, $3, $4::refund_status, 'CASH', $5, $6, 'тест возврата', $7)
            """,
            TENANT_A,
            payment_id,
            amount,
            status,
            SHIFT_A,
            USER_A_CASHIER,
            hashlib.sha256(uuid4().bytes).digest(),
        )

    with pytest.raises(asyncpg.CheckViolationError, match="превышает"):
        await refund(1001)
    await refund(400)
    with pytest.raises(asyncpg.CheckViolationError, match="превышает"):
        await refund(601)
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", payment_id) == "PAID"
    await refund(600)
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", payment_id) == "REFUNDED"
    with pytest.raises(asyncpg.CheckViolationError, match="превышает"):
        await refund(1)
