"""Возвраты (фаза 5 §4): лимит суммы в коде и в БД, частичные, REFUNDED производно, провайдерский возврат,
property-based: сумма возвратов никогда не превышает платёж."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from parking.modules.billing.application.contracts import Provider, RefundStatus
from parking.modules.billing.domain.payment import Payment, PaymentStatus, check_refund, refundable_amount
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.runtime.container import Container
from parking.shared.errors import ConflictError, ValidationError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal
from tests.support.billing_fixtures import FakeBilling, as_user, ensure_open_shift, make_obligation


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01R{uuid4().hex[:5].upper()}")


@pytest.fixture
async def manager(container: Container, users: dict[Role, Principal]) -> CurrentUser:
    actor = as_user(users[Role.MANAGER])
    await ensure_open_shift(container, actor, opening_float=1_000_000)
    return actor


async def test_refund_over_amount_rejected_in_code_and_in_db(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, manager: CurrentUser, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=None, reason=None)
    with pytest.raises(ValidationError) as exc:
        await container.refunds.request(
            manager, ob.payment_id, amount=ob.amount + 1, reason="слишком много", idempotency_key="k1"
        )
    assert exc.value.details["refundable"] == ob.amount
    # мимо кода — прямой INSERT: триггер app.refund_guard под FOR UPDATE платежа
    with pytest.raises(asyncpg.CheckViolationError):
        await owner.execute(
            "INSERT INTO refund (tenant_id, payment_id, amount, method, operator_id, reason, idempotency_key, shift_id) VALUES ($1, $2, $3, 'CASH', $4, 'direct', sha256('x'::bytea), (SELECT shift_id FROM payment WHERE id = $2))",
            TENANT_A,
            ob.payment_id,
            ob.amount + 1,
            manager.user_id,
        )
    first = await container.refunds.request(
        manager, ob.payment_id, amount=ob.amount - 100, reason="частичный", idempotency_key="k2"
    )
    assert first.status is RefundStatus.DONE
    assert (
        await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PAID"
    )  # частичный не меняет статус
    with pytest.raises(ValidationError):
        await container.refunds.request(manager, ob.payment_id, amount=101, reason="перебор", idempotency_key="k3")
    same = await container.refunds.request(
        manager, ob.payment_id, amount=ob.amount - 100, reason="частичный", idempotency_key="k2"
    )
    assert same.id == first.id  # идемпотентность по ключу
    last = await container.refunds.request(manager, ob.payment_id, amount=100, reason="остаток", idempotency_key="k4")
    assert last.status is RefundStatus.DONE
    assert (
        await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "REFUNDED"
    )  # производно триггером
    assert (
        await owner.fetchval("SELECT amount FROM payment WHERE id = $1", ob.payment_id) == ob.amount
    )  # платёж не мутирован
    with pytest.raises(ValidationError):
        await container.refunds.request(manager, ob.payment_id, amount=1, reason="ещё", idempotency_key="k5")
    print(
        f"\nGATE refund > amount: code → 422 (refundable={ob.amount}); DB trigger → CheckViolationError; REFUNDED derived"
    )


async def test_refund_requires_paid_payment(
    container: Container, owner: asyncpg.Connection, manager: CurrentUser, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    with pytest.raises(ConflictError):
        await container.refunds.request(
            manager, ob.payment_id, amount=100, reason="ещё не оплачен", idempotency_key=None
        )


async def test_provider_refund_executed_outside_transaction(
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    manager: CurrentUser,
    clean_lot: None,
) -> None:
    ob = await make_obligation(container, owner, _plate())
    await fake_billing.online.checkout(manager, ob.payment_id, Provider.PAYME, return_url=None)
    await owner.execute(
        "UPDATE payment SET status = 'PAID', method = 'ONLINE', provider = 'PAYME', paid_at = now() WHERE id = $1",
        ob.payment_id,
    )  # как после PerformTransaction
    refund = await fake_billing.refunds.request(
        manager, ob.payment_id, amount=ob.amount, reason="клиент передумал", idempotency_key="p1"
    )
    assert refund.status is RefundStatus.PENDING and refund.shift_id is None
    assert await fake_billing.refunds.process_pending(TENANT_A) == 1
    assert fake_billing.gateway.refund_calls == 1
    row = await owner.fetchrow("SELECT status::text AS status, provider_refund_id FROM refund WHERE id = $1", refund.id)
    assert row["status"] == "DONE" and row["provider_refund_id"] == "fake-refund-1"
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "REFUNDED"
    assert await fake_billing.refunds.process_pending(TENANT_A) == 0
    assert fake_billing.gateway.refund_calls == 1  # повторно не вызывается


@given(
    amount=st.integers(min_value=1, max_value=10_000_000),
    requests=st.lists(st.integers(min_value=1, max_value=10_000_000), min_size=1, max_size=30),
)
@settings(max_examples=300, deadline=None)
def test_property_refund_sum_never_exceeds_payment(amount: int, requests: list[int]) -> None:
    """Gate (property-based): при любой последовательности запросов сумма принятых возвратов ≤ сумме платежа."""
    payment = Payment(
        id=uuid4(),
        tenant_id=TENANT_A,
        lot_id=LOT_A1,
        session_id=uuid4(),
        amount=amount,
        status=PaymentStatus.PAID,
        idempotency_key=b"\0" * 32,
        calculation_snapshot={},
    )
    accepted: list[int] = []
    for req in requests:
        try:
            check_refund(payment, req, accepted)
        except ValidationError:
            assert sum(accepted) + req > amount
            continue
        accepted.append(req)
        assert sum(accepted) <= amount
    assert refundable_amount(amount, accepted) >= 0


async def test_property_refund_sum_in_database(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, manager: CurrentUser, clean_lot: None
) -> None:
    """То же свойство против триггера БД на случайных последовательностях (быстрый вариант hypothesis)."""
    import random

    rng = random.Random(42)
    for _ in range(3):
        ob = await make_obligation(container, owner, _plate())
        await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=None, reason=None)
        accepted = 0
        for i in range(8):
            amount = rng.randint(1, ob.amount)
            try:
                await container.refunds.request(
                    manager, ob.payment_id, amount=amount, reason="rnd", idempotency_key=f"rnd-{ob.payment_id}-{i}"
                )
                accepted += amount
            except ValidationError:
                assert accepted + amount > ob.amount
            total = await owner.fetchval(
                "SELECT coalesce(sum(amount), 0) FROM refund WHERE payment_id = $1 AND status <> 'FAILED'",
                ob.payment_id,
            )
            assert total == accepted <= ob.amount
