"""Эхо выезда: стрела одна на въезд и выезд — камера въезда видит выезжающую машину через секунды после камеры выезда
(MediaPark, 2026-10-15: почти каждый выезд давал «въезд»). Такой «въезд» не создаёт сессию, не закрывает выезжающую
сессию как REENTRY (вместе с оплатой) и не открывает стрелу. Окна ограничены: машина, вернувшаяся позже, въезжает
как обычно."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.parking.application.contracts import EntryStatus, ExitStatus, SagaState
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A
from tests.support.fakes import FakeBarrierDriver
from tests.support.parking_fixtures import entry_cmd, exit_cmd, subscription_decision


def _plate() -> PlateNumber:
    n = uuid4().int
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"
    return PlateNumber.parse(
        f"{10 + n % 90}{letters[n // 90 % 23]}{100 + n // 2070 % 900}{letters[n // 1863000 % 23]}{letters[n // 42849000 % 23]}"
    )


async def _sessions(owner: asyncpg.Connection, pl_session: UUID) -> list[str]:
    rows = await owner.fetch(
        "SELECT s.status::text AS status FROM parking_session s "
        "WHERE s.vehicle_id = (SELECT vehicle_id FROM parking_session WHERE id = $1) ORDER BY s.entry_time",
        pl_session,
    )
    return [r["status"] for r in rows]


async def test_entry_right_after_paid_exit_is_echo_and_keeps_payment(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    pl = _plate()
    now = datetime.now(tz=UTC)
    entered = await container.parking.process_entry(entry_cmd(pl, at=now - timedelta(hours=2)))
    exited = await container.parking.start_exit(exit_cmd(pl, at=now))
    assert exited.status is ExitStatus.PAYMENT_REQUIRED and exited.saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, exited.saga_id) is SagaState.PAYMENT_REQUIRED

    echo = await container.parking.process_entry(entry_cmd(pl, at=now + timedelta(seconds=2)))
    later = await container.parking.process_entry(entry_cmd(pl, at=now + timedelta(minutes=10)))  # кассир не спешит

    assert echo.status is EntryStatus.ENTRY_EXIT_ECHO and echo.session_id == entered.session_id
    assert later.status is EntryStatus.ENTRY_EXIT_ECHO and echo.barrier_command_id is None
    assert entered.session_id is not None
    assert await _sessions(owner, entered.session_id) == ["ACTIVE"], "ни новой сессии, ни REENTRY"
    saga = await owner.fetchval("SELECT state::text FROM exit_saga WHERE id = $1", exited.saga_id)
    assert saga == "PAYMENT_REQUIRED", "выезд ждёт оплату дальше"
    assert await container.occupancy.get(LOT_A1) == 1


async def test_entry_right_after_completed_exit_is_echo_but_return_later_is_entry(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    pl = _plate()
    now = datetime.now(tz=UTC)
    entered = await container.parking.process_entry(entry_cmd(pl, at=now - timedelta(hours=2)))
    exited = await container.parking.start_exit(exit_cmd(pl, at=now, decision=subscription_decision()))
    assert exited.saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, exited.saga_id) is SagaState.BARRIER_OPENING
    saga = await container.parking.get_saga_by_id(TENANT_A, exited.saga_id)
    assert saga.barrier_command_id is not None
    await executor[0].execute(TENANT_A, saga.barrier_command_id)
    await container.parking.advance_saga(TENANT_A, exited.saga_id)
    assert entered.session_id is not None
    assert await _sessions(owner, entered.session_id) == ["CLOSED"]

    echo = await container.parking.process_entry(entry_cmd(pl, at=now + timedelta(seconds=3)))
    assert echo.status is EntryStatus.ENTRY_EXIT_ECHO and echo.session_id == entered.session_id
    assert await _sessions(owner, entered.session_id) == ["CLOSED"]

    back = await container.parking.process_entry(entry_cmd(pl, at=now + timedelta(minutes=2)))
    assert back.status is EntryStatus.ENTRY_CONFIRMED and back.barrier_command_id is not None
    assert await _sessions(owner, entered.session_id) == ["CLOSED", "ACTIVE"]


async def test_entry_right_after_exit_without_session_is_echo(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    """Въезд не был распознан (камера выключена): выезд без въезда → разбор кассиром; «въезд» следом — эхо."""
    pl = _plate()
    now = datetime.now(tz=UTC)
    exited = await container.parking.start_exit(exit_cmd(pl, at=now))
    assert exited.status is ExitStatus.EXIT_WITHOUT_SESSION and exited.session_id is not None

    echo = await container.parking.process_entry(entry_cmd(pl, at=now + timedelta(seconds=2)))

    assert echo.status is EntryStatus.ENTRY_EXIT_ECHO and echo.session_id == exited.session_id
    assert await _sessions(owner, exited.session_id) == ["MANUAL_REVIEW"]
