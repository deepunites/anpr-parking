"""Фикстуры фазы 5: мерчант-аккаунты, кассир со сменой, обязательство саги, сервисы с фейковым провайдером."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
import pytest

from parking.modules.billing.application.contracts import CashierShift
from parking.modules.billing.application.online import OnlinePaymentService
from parking.modules.billing.application.reconciliation import ReconciliationService
from parking.modules.billing.application.refunds import RefundService
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import ExitStatus
from parking.runtime.container import Container, build_billing_repos
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal
from tests.support.fakes import FakeGateway, FakeGatewayRegistry
from tests.support.parking_fixtures import entry_cmd, exit_cmd

PAYME_MERCHANT = "m-alpha-test"
PAYME_KEY = "PaymeSecretKey-9f8e7d6c5b4a-DO-NOT-LOG"
CLICK_SERVICE_ID = "31337"
CLICK_SECRET = "ClickSecretKey-1a2b3c4d5e6f-DO-NOT-LOG"
CLICK_USER_ID = "4242"
TEST_SECRETS = {
    "kv/tenants/alpha/barriers/e102": "test-bearer-secret",
    "kv/tenants/alpha/payme": json.dumps({"key": PAYME_KEY}),
    "kv/tenants/alpha/click": json.dumps(
        {"service_id": CLICK_SERVICE_ID, "merchant_user_id": CLICK_USER_ID, "secret_key": CLICK_SECRET}
    ),
}


@pytest.fixture
async def provider_accounts(owner: asyncpg.Connection) -> None:
    await owner.execute(
        """
        INSERT INTO provider_account (tenant_id, provider, merchant_id, secret_ref) VALUES
          ($1, 'PAYME', $2, 'kv/tenants/alpha/payme'), ($1, 'CLICK', $3, 'kv/tenants/alpha/click')
        ON CONFLICT (tenant_id, provider) DO UPDATE SET merchant_id = EXCLUDED.merchant_id, secret_ref = EXCLUDED.secret_ref, active = true
        """,
        TENANT_A,
        PAYME_MERCHANT,
        CLICK_SERVICE_ID,
    )


async def ensure_open_shift(
    container: Container, actor: CurrentUser, lot_id: UUID = LOT_A1, *, opening_float: int = 100_000
) -> CashierShift:
    """Открытая смена актора на площадке (переоткрывает, если смена на другой площадке)."""
    current = await container.cashier.current_shift(actor)
    if current is not None and current.lot_id != lot_id:
        await container.cashier.close_shift(
            actor, current.id, counted_cash=current.opening_float, notes="fixture reset"
        )
        current = None
    if current is None:
        current = await container.cashier.open_shift(actor, lot_id, opening_float=opening_float, notes="fixture")
    return current


async def close_shift_if_open(container: Container, actor: CurrentUser) -> None:
    current = await container.cashier.current_shift(actor)
    if current is not None:
        await container.cashier.close_shift(
            actor, current.id, counted_cash=current.opening_float, notes="fixture teardown"
        )


def as_user(p: Principal) -> CurrentUser:
    return CurrentUser(user_id=p.user_id, tenant_id=p.tenant_id, role=p.role, lot_scope=frozenset())


@pytest.fixture
async def cashier(container: Container, users: dict[Role, Principal]) -> AsyncIterator[CurrentUser]:
    """Кассир с открытой сменой на LOT_A1; смена закрывается после теста."""
    actor = as_user(users[Role.CASHIER])
    current = await container.cashier.current_shift(actor)
    if current is not None and current.lot_id != LOT_A1:
        await container.cashier.close_shift(
            actor, current.id, counted_cash=current.opening_float, notes="fixture reset"
        )
        current = None
    if current is None:
        await container.cashier.open_shift(actor, LOT_A1, opening_float=100_000, notes="fixture")
    yield actor
    current = await container.cashier.current_shift(actor)
    if current is not None:
        await container.cashier.close_shift(
            actor, current.id, counted_cash=current.opening_float, notes="fixture teardown"
        )


@dataclass(frozen=True, slots=True)
class Obligation:
    session_id: UUID
    saga_id: UUID
    payment_id: UUID
    amount: int
    plate: PlateNumber


async def make_obligation(
    container: Container, owner: asyncpg.Connection, plate: PlateNumber, *, hours: int = 2
) -> Obligation:
    """ENTRY 2 ч назад → EXIT сейчас → PAYMENT_REQUIRED → шаг саги создаёт PENDING-обязательство."""
    entry_at = datetime.now(tz=UTC) - timedelta(hours=hours)
    entered = await container.parking.process_entry(entry_cmd(plate, at=entry_at))
    assert entered.session_id is not None
    result = await container.parking.start_exit(exit_cmd(plate, at=datetime.now(tz=UTC)))
    assert result.status is ExitStatus.PAYMENT_REQUIRED and result.saga_id is not None
    await container.parking.advance_saga(TENANT_A, result.saga_id)
    row = await owner.fetchrow("SELECT id, amount FROM payment WHERE saga_id = $1", result.saga_id)
    assert row is not None
    return Obligation(entered.session_id, result.saga_id, row["id"], int(row["amount"]), plate)


@dataclass(slots=True)
class FakeBilling:
    gateway: FakeGateway
    online: OnlinePaymentService
    refunds: RefundService
    reconciliation: ReconciliationService


@pytest.fixture
def fake_billing(container: Container) -> FakeBilling:
    registry = FakeGatewayRegistry()
    return FakeBilling(
        gateway=registry.gateway,
        online=OnlinePaymentService(
            uow_factory=container.uow, repos=build_billing_repos, gateways=registry, clock=container.clock
        ),
        refunds=RefundService(
            uow_factory=container.uow, repos=build_billing_repos, gateways=registry, clock=container.clock
        ),
        reconciliation=ReconciliationService(
            uow_factory=container.uow, repos=build_billing_repos, gateways=registry, clock=container.clock
        ),
    )
