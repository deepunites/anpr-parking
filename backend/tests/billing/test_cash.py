"""Наличные и смены (фаза 5 §3): подтверждение обязательства, предоплата, сверка суммы, смена, EXIT_INFERRED, I-6 в БД."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from parking.modules.billing.application.contracts import PaymentStatus
from parking.modules.billing.infrastructure.repositories import SqlPaymentRepository
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import ExitStatus, SagaState
from parking.runtime.container import Container
from parking.shared.errors import ConflictError, ValidationError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal, bearer
from tests.support.billing_fixtures import as_user, close_shift_if_open, ensure_open_shift, make_obligation
from tests.support.parking_fixtures import entry_cmd, exit_cmd


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01C{uuid4().hex[:5].upper()}")


async def test_cash_confirm_settles_saga_and_is_idempotent(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    paid = await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=ob.amount, reason=None)
    assert paid.status is PaymentStatus.PAID and paid.method is not None and paid.method.value == "CASH"
    assert paid.operator_id == cashier.user_id and paid.shift_id is not None
    again = await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=ob.amount, reason=None)
    assert again.id == paid.id and again.status is PaymentStatus.PAID  # двойная отправка → одно списание
    assert await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1", ob.session_id) == 1
    assert await owner.fetchval("SELECT count(*) FROM payment_attempt WHERE payment_id = $1", ob.payment_id) == 1
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'PAYMENT_CASH' AND entity_id = $1", ob.payment_id
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.payment.confirmed.v1' AND aggregate_id = $1",
            ob.payment_id,
        )
        == 1
    )
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.BARRIER_OPENING


async def test_declared_amount_mismatch_is_refused(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    """Gate: сумма, присланная клиентом и отличная от посчитанной → отказ (кассир без права на нестандартную сумму)."""
    ob = await make_obligation(container, owner, _plate())
    with pytest.raises(ValidationError) as exc:
        await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=ob.amount - 100, reason="скидка")
    assert exc.value.code == "AMOUNT_MISMATCH" and exc.value.details["computed"] == ob.amount
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PENDING"


async def test_cash_requires_open_shift_on_the_same_lot(
    container: Container, owner: asyncpg.Connection, users: dict[Role, Principal], clean_lot: None
) -> None:
    operator = as_user(users[Role.OPERATOR])  # смены нет (и права открыть — тоже)
    ob = await make_obligation(container, owner, _plate())
    with pytest.raises(ConflictError) as exc:
        await container.cashier.confirm_cash(operator, ob.payment_id, declared_amount=None, reason=None)
    assert exc.value.code == "NO_OPEN_SHIFT"


async def test_prepay_then_exit_is_free_and_custom_amount_needs_permission(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, users: dict[Role, Principal], clean_lot: None
) -> None:
    pl = _plate()
    entry_at = datetime.now(tz=UTC) - timedelta(hours=3)
    session_id = (await container.parking.process_entry(entry_cmd(pl, at=entry_at))).session_id
    assert session_id is not None
    quote = await container.cashier.quote(cashier, session_id)
    assert quote > 0
    with pytest.raises(ValidationError):  # кассир: нестандартная сумма запрещена
        await container.cashier.prepay(cashier, session_id, declared_amount=quote // 2, reason="постоянный клиент")
    paid = await container.cashier.prepay(cashier, session_id, declared_amount=quote, reason=None)
    assert paid.status is PaymentStatus.PAID and paid.amount == quote and paid.paid_until is not None
    same = await container.cashier.prepay(cashier, session_id, declared_amount=quote, reason=None)
    assert same.id == paid.id  # повтор в окне выезда — тот же платёж
    exit_ = await container.parking.start_exit(exit_cmd(pl, at=datetime.now(tz=UTC)))
    assert exit_.status is ExitStatus.EXIT_CONFIRMED and exit_.amount_minor == 0  # SD-2b: выезд без доплаты

    # менеджер с payment:create_cash_custom и reason — можно, но у него должна быть своя смена
    manager = as_user(users[Role.MANAGER])
    pl2 = _plate()
    s2 = (await container.parking.process_entry(entry_cmd(pl2, at=entry_at))).session_id
    assert s2 is not None
    await ensure_open_shift(container, manager, opening_float=0)
    try:
        custom = await container.cashier.prepay(
            manager, s2, declared_amount=1000, reason="компенсация за поломку шлагбаума"
        )
        assert custom.amount == 1000 and custom.calculation_snapshot["custom_amount"]["computed"] > 1000
        audit = await owner.fetchrow(
            "SELECT reason FROM audit_log WHERE action = 'PAYMENT_CASH_PREPAID' AND entity_id = $1", custom.id
        )
        assert audit is not None and "компенсация" in audit["reason"]
    finally:
        await close_shift_if_open(container, manager)


async def test_paid_but_left_through_other_exit_closes_session_as_inferred(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    """§6: водитель уехал через соседний выезд после оплаты → сессия закрыта по платежу, exit_time = paid_at."""
    pl = _plate()
    session_id = (
        await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=2)))
    ).session_id
    assert session_id is not None
    paid = await container.cashier.prepay(cashier, session_id, declared_amount=None, reason=None)
    assert await container.parking.sweep_prepaid(TENANT_A) == 0  # окно выезда ещё открыто
    later = datetime.now(tz=UTC) + timedelta(hours=2)  # exit_grace 15 мин + exit_inferred_after 60 мин прошли
    assert await container.parking.sweep_prepaid(TENANT_A, now=later) == 1
    row = await owner.fetchrow(
        "SELECT status::text AS status, exit_kind::text AS kind, exit_time, closed_reason FROM parking_session WHERE id = $1",
        session_id,
    )
    assert (
        row["status"] == "CLOSED" and row["kind"] == "INFERRED_BY_PAYMENT" and row["closed_reason"] == "EXIT_INFERRED"
    )
    paid_at = await owner.fetchval("SELECT paid_at FROM payment WHERE id = $1", paid.id)
    assert row["exit_time"] == paid_at
    assert await container.parking.sweep_prepaid(TENANT_A, now=later) == 0  # идемпотентно


async def test_shift_close_computes_expected_cash_and_discrepancy(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, users: dict[Role, Principal], clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=None, reason=None)
    shift = await container.cashier.current_shift(cashier)
    assert shift is not None
    closed = await container.cashier.close_shift(
        cashier, shift.id, counted_cash=100_000 + ob.amount - 300, notes="не хватает 300"
    )
    assert closed.expected_cash == 100_000 + ob.amount and closed.discrepancy == -300
    row = await owner.fetchrow(
        "SELECT expected_cash, counted_cash, discrepancy FROM cashier_shift WHERE id = $1", shift.id
    )
    assert (row["expected_cash"], row["discrepancy"]) == (closed.expected_cash, -300)
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.shift.closed.v1' AND aggregate_id = $1", shift.id
        )
        == 1
    )
    with pytest.raises(ConflictError):
        await container.cashier.close_shift(cashier, shift.id, counted_cash=0, notes=None)
    await container.cashier.open_shift(cashier, LOT_A1, opening_float=100_000, notes="fixture restore")

    # наличный возврат — из открытой смены того, кто возвращает (менеджер с payment:refund), уменьшает её ожидаемую кассу
    manager = as_user(users[Role.MANAGER])
    m_shift = await container.cashier.open_shift(manager, LOT_A1, opening_float=10_000, notes="refund desk")
    refund = await container.refunds.request(
        manager, ob.payment_id, amount=500, reason="сдача ошибочно удержана", idempotency_key=None
    )
    assert refund.status.value == "DONE" and refund.shift_id == m_shift.id
    m_closed = await container.cashier.close_shift(manager, m_shift.id, counted_cash=9_500, notes=None)
    assert m_closed.expected_cash == 9_500 and m_closed.discrepancy == 0


async def test_paid_payment_cannot_be_updated_at_db_level(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    """Gate: UPDATE платежа в статусе PAID отклоняется триггером БД (I-6), даже через репозиторий."""
    ob = await make_obligation(container, owner, _plate())
    paid = await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=None, reason=None)
    paid.amount += 1
    with pytest.raises(DBAPIError) as exc:
        async with container.uow(TENANT_A) as uow:
            await SqlPaymentRepository(uow.session).save(paid)
    assert "I-6" in str(exc.value)
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute("UPDATE payment SET amount = amount + 1 WHERE id = $1", paid.id)
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute("UPDATE payment SET status = 'PENDING' WHERE id = $1", paid.id)
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute("DELETE FROM payment WHERE id = $1", paid.id)
    print("\nGATE I-6: UPDATE/DELETE PAID payment rejected by trigger app.payment_guard")


async def test_shift_api_flow(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    headers = await bearer(client, users[Role.CASHIER])
    current = await client.get("/api/v1/shifts/current", headers=headers)
    if current.status_code == 200:
        await client.post(
            f"/api/v1/shifts/{current.json()['id']}/close",
            json={"counted_cash": current.json()["opening_float"]},
            headers=headers,
        )
    opened = await client.post("/api/v1/shifts", json={"lot_id": str(LOT_A1), "opening_float": 5000}, headers=headers)
    assert opened.status_code == 201, opened.text
    duplicate = await client.post("/api/v1/shifts", json={"lot_id": str(LOT_A1)}, headers=headers)
    assert duplicate.status_code == 409
    closed = await client.post(
        f"/api/v1/shifts/{opened.json()['id']}/close",
        json={"counted_cash": 4900, "notes": "минус 100"},
        headers=headers,
    )
    assert closed.status_code == 200 and closed.json()["discrepancy"] == -100 and closed.json()["status"] == "CLOSED"
