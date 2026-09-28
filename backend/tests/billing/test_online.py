"""Онлайн-оплата через провайдера (фаза 5 §3, §6): попытка до вызова, без повторного create(), верификация."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from parking.modules.billing.application.contracts import AttemptStatus, PaymentStatus, Provider
from parking.modules.billing.domain.reconciliation import RemoteState
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import SagaState
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import TENANT_A
from tests.support.api_fixtures import Principal
from tests.support.billing_fixtures import FakeBilling, as_user, make_obligation
from tests.support.fakes import FakeBarrierDriver, fake_registry, no_sleep


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01O{uuid4().hex[:5].upper()}")


@pytest.fixture
def operator(users: dict[Role, Principal]) -> CurrentUser:
    return as_user(users[Role.OPERATOR])


async def test_checkout_twice_creates_one_provider_intent(
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    operator: CurrentUser,
    clean_lot: None,
) -> None:
    """Gate: двойная отправка одного платежа → одно списание (один create() у провайдера, одна попытка)."""
    ob = await make_obligation(container, owner, _plate())
    first = await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    second = await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    assert fake_billing.gateway.create_calls == 1
    assert (
        first.attempt_id == second.attempt_id
        and first.checkout_url == second.checkout_url == "https://checkout.fake/pay/1"
    )
    assert first.status is AttemptStatus.PENDING and first.provider_transaction_id == "fake-tx-1"
    attempts = await owner.fetch(
        "SELECT status::text AS status, provider_transaction_id, idempotency_key FROM payment_attempt WHERE payment_id = $1",
        ob.payment_id,
    )
    assert len(attempts) == 1 and attempts[0]["provider_transaction_id"] == "fake-tx-1"
    assert bytes(attempts[0]["idempotency_key"]) == await owner.fetchval(
        "SELECT idempotency_key FROM payment WHERE id = $1", ob.payment_id
    )
    request = fake_billing.gateway.created[0]
    assert request.idempotency_key == str(ob.payment_id) and request.amount == ob.amount  # type: ignore[attr-defined]


async def test_provider_timeout_does_not_recreate_and_parks_saga_for_verification(
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    operator: CurrentUser,
    clean_lot: None,
) -> None:
    """Gate: таймаут провайдера → не второй create(), а status(); неизвестно → UNKNOWN, сага PAYMENT_PENDING_VERIFICATION."""
    ob = await make_obligation(container, owner, _plate())
    fake_billing.gateway.create_mode = "timeout"
    fake_billing.gateway.status_state = None
    first = await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    assert (
        first.status is AttemptStatus.UNKNOWN
        and fake_billing.gateway.create_calls == 1
        and fake_billing.gateway.status_calls == 1
    )
    again = await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    assert again.attempt_id == first.attempt_id and fake_billing.gateway.create_calls == 1  # ретрай create запрещён
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PENDING"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.payment.unverified.v1' AND aggregate_id = $1",
            ob.payment_id,
        )
        == 1
    )
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.PAYMENT_PENDING_VERIFICATION
    assert (
        await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.PAYMENT_PENDING_VERIFICATION
    )  # машина не выпускается
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.session.manual_review.v1' AND aggregate_id = $1",
            ob.session_id,
        )
        == 1
    )
    # джоб верификации: провайдер наконец отвечает PAID → платёж PAID без нового create, сага едет дальше
    fake_billing.gateway.status_state = RemoteState.PAID
    assert await fake_billing.online.verify_unknown(TENANT_A) == 1
    assert fake_billing.gateway.create_calls == 1
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PAID"
    assert (
        await owner.fetchval("SELECT status::text FROM payment_attempt WHERE id = $1", first.attempt_id) == "SUCCEEDED"
    )
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.BARRIER_OPENING
    print(
        "\nGATE provider timeout: create_calls=1, status_calls≥1, saga PAYMENT_PENDING_VERIFICATION → PAID via status()"
    )


async def test_timeout_with_status_paid_confirms_immediately(
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    operator: CurrentUser,
    clean_lot: None,
) -> None:
    ob = await make_obligation(container, owner, _plate())
    fake_billing.gateway.create_mode = "timeout"
    fake_billing.gateway.status_state = RemoteState.PAID
    result = await fake_billing.online.checkout(operator, ob.payment_id, Provider.CLICK, return_url=None)
    assert result.status is AttemptStatus.SUCCEEDED
    assert (
        await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PENDING"
    )  # подтверждает джоб/вебхук, не догадка
    fake_billing.gateway.status_state = RemoteState.PAID
    # попытка уже SUCCEEDED (провайдер сказал PAID): подтверждение платежа — через verify по UNKNOWN не пойдёт; проверяем через статус-джоб на UNKNOWN нет
    assert fake_billing.gateway.create_calls == 1


async def test_provider_rejection_fails_payment_and_saga_goes_to_review(
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    operator: CurrentUser,
    clean_lot: None,
) -> None:
    ob = await make_obligation(container, owner, _plate())
    fake_billing.gateway.create_mode = "error"
    result = await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    assert result.status is AttemptStatus.FAILED
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "FAILED"
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.MANUAL_REVIEW
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.payment.failed.v1' AND aggregate_id = $1",
            ob.payment_id,
        )
        == 1
    )


async def test_barrier_failure_after_payment_keeps_payment(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    """Gate/§6: платёж прошёл, шлагбаум не открылся → платёж остаётся PAID, сага MANUAL_REVIEW, возврата нет."""
    from parking.modules.devices.application.commands import BarrierCommandExecutor
    from parking.runtime.container import build_device_repos

    ob = await make_obligation(container, owner, _plate())
    await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=None, reason=None)
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.BARRIER_OPENING
    saga = await container.parking.get_saga_by_id(TENANT_A, ob.saga_id)
    assert saga.barrier_command_id is not None
    registry, _ = fake_registry(FakeBarrierDriver(results=[False]))
    failing = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )
    await failing.execute(TENANT_A, saga.barrier_command_id)
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.MANUAL_REVIEW
    row = await owner.fetchrow("SELECT status::text AS status, amount FROM payment WHERE id = $1", ob.payment_id)
    assert row["status"] == "PAID" and row["amount"] == ob.amount
    assert await owner.fetchval("SELECT count(*) FROM refund WHERE payment_id = $1", ob.payment_id) == 0
    assert (
        await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", ob.session_id) == "MANUAL_REVIEW"
    )
    print("\nGATE barrier failure after payment: payment PAID kept, no refund, saga MANUAL_REVIEW")


async def test_checkout_requires_pending_and_configured_provider(
    container: Container,
    owner: asyncpg.Connection,
    fake_billing: FakeBilling,
    operator: CurrentUser,
    cashier: CurrentUser,
    clean_lot: None,
) -> None:
    from parking.shared.errors import ConflictError, ServiceUnavailableError

    ob = await make_obligation(container, owner, _plate())
    await owner.execute("DELETE FROM provider_account WHERE tenant_id = $1", TENANT_A)
    with pytest.raises(ServiceUnavailableError):
        await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=None, reason=None)
    with pytest.raises(ConflictError):
        await fake_billing.online.checkout(operator, ob.payment_id, Provider.PAYME, return_url=None)
    assert fake_billing.gateway.create_calls == 0
    assert (
        await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id)
    ) == PaymentStatus.PAID.value
