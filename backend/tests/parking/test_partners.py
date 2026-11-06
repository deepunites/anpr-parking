"""Партнёр площадки (MediaPark, 2026-11-06): компьютерный клуб выпускает гостей. Бармен находит машину по номеру и
отмечает «за счёт клуба» — на выезде 0 сум и стрела сама; отметка, когда машина уже ждёт оплату у выезда, отменяет
обязательство и открывает стрелу. Раз в месяц — расчёт: клуб платит фиксированную сумму в месяц (отменённые
отметки не в счёт машин; доплата за машину — только если задана)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.identity.application.user_service import CreateUserCommand
from parking.modules.parking.application.contracts import ExitStatus, SagaState
from parking.runtime.container import Container
from parking.shared.errors import AuthorizationError, ConflictError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.fakes import FakeBarrierDriver
from tests.support.parking_fixtures import entry_cmd, exit_cmd

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
FEE = 50_000_000  # 500 000 сум в месяц — плата клуба


def _plate() -> PlateNumber:
    n = uuid4().int
    letters = "ABEHKMNPTXYZ"
    return PlateNumber.parse(
        f"90{letters[n % 12]}{100 + n // 12 % 900}{letters[n // 10800 % 12]}{letters[n // 129600 % 12]}"
    )


async def _club(container: Container) -> tuple[UUID, CurrentUser]:
    partner = await container.partners.create_partner(
        ADMIN,
        lot_id=LOT_A1,
        name=f"Клуб {uuid4().hex[:6]}",
        price_per_car=0,
        monthly_fee=FEE,
        reason="компьютерный клуб",
    )
    user = await container.users.create(
        ADMIN,
        CreateUserCommand(
            email=f"bar.{uuid4().hex[:8]}@alpha-parking.uz",
            password="correct-horse-battery-staple",
            role=Role.PARTNER,
            display_name="Бармен",
            lot_scope=frozenset({LOT_A1}),
        ),
        reason="вход бармена",
    )
    await container.partners.add_member(ADMIN, partner.id, user.id)
    bartender = CurrentUser(user_id=user.id, tenant_id=TENANT_A, role=Role.PARTNER, lot_scope=frozenset({LOT_A1}))
    return partner.id, bartender


async def _enter(container: Container, pl: PlateNumber, hours_ago: float = 2.0) -> UUID:
    entered = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=hours_ago)))
    assert entered.session_id is not None
    return entered.session_id


async def test_guest_of_the_club_exits_free_and_the_barrier_opens(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    partner_id, bartender = await _club(container)
    pl = _plate()
    session_id = await _enter(container, pl)

    cars = await container.partners.find_cars(bartender, pl.value)
    assert [c.session_id for c in cars] == [session_id] and cars[0].exact and not cars[0].validated
    validation_id = await container.partners.validate(bartender, session_id)

    exited = await container.parking.start_exit(exit_cmd(pl))
    assert exited.status is ExitStatus.EXIT_CONFIRMED and exited.amount_minor == 0, "2 часа — за счёт клуба"
    assert exited.saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, exited.saga_id) is SagaState.BARRIER_OPENING
    payment = await owner.fetchrow(
        "SELECT method::text AS method, amount, source_id FROM payment WHERE session_id = $1", session_id
    )
    assert payment is not None and payment["method"] == "PARTNER" and payment["amount"] == 0
    assert payment["source_id"] == validation_id
    month = await container.partners.report(ADMIN, partner_id, date.today().replace(day=1))
    assert month.count == 1 and month.total == FEE and month.cars_total == 0 and month.items[0].plate == pl.value


async def test_validation_while_the_car_waits_to_pay_opens_the_barrier(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    _, bartender = await _club(container)
    pl = _plate()
    session_id = await _enter(container, pl)
    exited = await container.parking.start_exit(exit_cmd(pl))
    assert exited.saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, exited.saga_id) is SagaState.PAYMENT_REQUIRED

    await container.partners.validate(bartender, session_id)

    saga = await owner.fetchval("SELECT state::text FROM exit_saga WHERE id = $1", exited.saga_id)
    assert saga == "BARRIER_OPENING", "клуб отметил у выезда — стрела открывается сразу"
    statuses = await owner.fetch(
        "SELECT method::text AS method, status::text AS status FROM payment WHERE session_id = $1 ORDER BY created_at",
        session_id,
    )
    assert [(r["method"], r["status"]) for r in statuses][-1] == ("PARTNER", "PAID")
    assert any(r["status"] == "CANCELLED" for r in statuses), "обязательство водителя отменено"


async def test_cancelled_validation_is_not_billed_and_double_validation_is_refused(
    container: Container, clean_lot: None
) -> None:
    partner_id, bartender = await _club(container)
    first, second = _plate(), _plate()
    a = await _enter(container, first)
    b = await _enter(container, second)
    va = await container.partners.validate(bartender, a)
    await container.partners.validate(bartender, b)
    with pytest.raises(ConflictError):
        await container.partners.validate(bartender, b)

    await container.partners.cancel(bartender, va, reason="ошиблись машиной")

    month = await container.partners.my_month(bartender, date.today().replace(day=1))
    assert month.count == 1 and month.total == FEE, "плата в месяц — сколько бы машин ни было"
    assert {i.state for i in month.items} == {"CANCELLED", "PARKED"}
    exited = await container.parking.start_exit(exit_cmd(first))
    assert exited.amount_minor > 0, "отметка отменена — по тарифу"
    assert (await container.partners.report(ADMIN, partner_id, date.today().replace(day=1))).count == 1


async def test_misread_plate_is_found_and_strangers_are_not_partners(container: Container, clean_lot: None) -> None:
    _, bartender = await _club(container)
    pl = _plate()
    session_id = await _enter(container, pl)
    misread = "70" + pl.value[2:]  # камера въезда спутала регион

    cars = await container.partners.find_cars(bartender, misread)

    assert [c.session_id for c in cars] == [session_id] and not cars[0].exact
    cashier = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.CASHIER, lot_scope=frozenset())
    with pytest.raises(AuthorizationError):
        await container.partners.find_cars(cashier, pl.value)


async def test_optional_price_per_car_adds_to_the_monthly_fee(container: Container, clean_lot: None) -> None:
    partner_id, bartender = await _club(container)
    await container.partners.update_partner(
        ADMIN, partner_id, name=None, price_per_car=200_000, active=None, reason="доплата за машину"
    )
    for _ in range(2):
        await container.partners.validate(bartender, await _enter(container, _plate()))

    month = await container.partners.report(ADMIN, partner_id, date.today().replace(day=1))

    assert month.count == 2 and month.cars_total == 400_000 and month.total == FEE + 400_000
