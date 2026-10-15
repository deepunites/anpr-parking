"""Разбор выезда у стрелы (MediaPark, 2026-10-15, ночь): камера выезда читала номера с ошибкой (90D077DD → 90D07CD),
выезд не находил въезд, а «Открыть выезд» в карточке отвечал ошибкой — машины не выпускались.

«Открыть выезд» без начатого выезда начинает обычный выезд по тарифу (к оплате — кассир принимает деньги, стрела
откроется сама; «выезд без въезда» — 0 минут, стрела сразу). Повторное чтение того же ошибочного номера не выпускает
машину бесплатно. Похожие номера среди машин на площадке — и «Это она»: разбор отменяется, у настоящей машины
начинается выезд с момента, когда её увидела камера. Всё — с причиной и в аудите (I-15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import ExitStatus, SagaState
from parking.modules.parking.domain.similar import plate_distance
from parking.runtime.container import Container
from parking.shared.errors import ConflictError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.fakes import FakeBarrierDriver
from tests.support.parking_fixtures import entry_cmd, exit_cmd

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
LETTERS = "ABEHKMNPTXYZ"  # без пар, которые OCR путает между собой: кандидаты в тесте не похожи друг на друга


def _plate() -> PlateNumber:
    n = uuid4().int
    return PlateNumber.parse(
        f"{10 + n % 80}{LETTERS[n // 80 % 12]}{100 + n // 960 % 900}{LETTERS[n // 864000 % 12]}{LETTERS[n // 10368000 % 12]}"
    )


def _misread(pl: PlateNumber) -> PlateNumber:
    """Как ночью на MediaPark: потерян символ и спутана буква (90D077DD → 90D07CD)."""
    v = pl.value
    return PlateNumber.parse(v[:5] + v[6] + ("C" if v[7] != "C" else "W"))


async def _enter(container: Container, pl: PlateNumber, hours_ago: float = 2.0) -> UUID:
    result = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=hours_ago)))
    assert result.session_id is not None
    return result.session_id


async def _audit(owner: asyncpg.Connection, session_id: UUID, action: str) -> int:
    return int(
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE session_id = $1 AND action = $2 AND user_id IS NOT NULL",
            session_id,
            action,
        )
    )


async def test_open_exit_releases_exit_without_entry(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    unknown = await container.parking.start_exit(exit_cmd(_plate()))
    assert unknown.status is ExitStatus.EXIT_WITHOUT_SESSION and unknown.session_id is not None
    assert await container.occupancy.get(LOT_A1) == 1

    view = await container.parking.manual_open_exit(ADMIN, unknown.session_id, reason="въезд не распознан, выпускаю")

    assert view.amount_due == 0, "«выезд без въезда» — 0 минут с момента, когда камера увидела машину"
    assert await container.parking.advance_saga(TENANT_A, view.id) is SagaState.BARRIER_OPENING
    saga = await container.parking.get_saga_by_id(TENANT_A, view.id)
    assert saga.barrier_command_id is not None
    await executor[0].execute(TENANT_A, saga.barrier_command_id)
    assert await container.parking.advance_saga(TENANT_A, view.id) is SagaState.COMPLETED
    status = await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", unknown.session_id)
    assert status == "CLOSED" and await container.occupancy.get(LOT_A1) == 0
    assert await _audit(owner, unknown.session_id, "EXIT_STARTED_MANUAL") == 1


async def test_open_exit_for_parked_car_asks_for_payment(container: Container, clean_lot: None) -> None:
    session_id = await _enter(container, _plate())

    view = await container.parking.manual_open_exit(ADMIN, session_id, reason="камера выезда не сработала")

    assert view.amount_due > 0, "стоянка 2 часа — к оплате, бесплатно не выпускаем"
    assert await container.parking.advance_saga(TENANT_A, view.id) is SagaState.PAYMENT_REQUIRED
    with pytest.raises(ConflictError) as again:
        await container.parking.manual_open_exit(ADMIN, session_id, reason="ещё раз")
    assert again.value.code == "EXIT_IN_PROGRESS"


async def test_same_misread_again_does_not_release_for_free(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    wrong = _plate()
    now = datetime.now(tz=UTC)
    first = await container.parking.start_exit(exit_cmd(wrong, at=now))
    again = await container.parking.start_exit(exit_cmd(wrong, at=now + timedelta(seconds=40)))

    assert again.status is ExitStatus.EXIT_WITHOUT_SESSION and again.session_id == first.session_id
    assert again.saga_id is None
    assert await owner.fetchval("SELECT count(*) FROM exit_saga WHERE session_id = $1", first.session_id) == 0


async def test_similar_plates_and_match_start_exit_of_the_parked_car(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    real = _plate()
    parked = await _enter(container, real)
    others = [await _enter(container, _plate(), hours_ago=1) for _ in range(3)]
    seen_at = datetime.now(tz=UTC) - timedelta(minutes=3)
    unknown = await container.parking.start_exit(exit_cmd(_misread(real), at=seen_at))
    assert unknown.session_id is not None and unknown.status is ExitStatus.EXIT_WITHOUT_SESSION
    assert await container.occupancy.get(LOT_A1) == 5

    similar = await container.parking.similar_sessions(ADMIN, unknown.session_id)

    assert similar and similar[0].session.id == parked and similar[0].distance <= 3
    assert similar[0].session.plate is not None
    assert all(s.session.id != unknown.session_id for s in similar)
    assert not {s.session.id for s in similar} & set(others) or all(
        plate_distance(_misread(real).value, s.session.plate or "") <= 3 for s in similar
    )

    view = await container.parking.match_exit(
        ADMIN, unknown.session_id, parked, reason="номер на выезде прочитан с ошибкой"
    )

    assert view.session_id == parked and view.amount_due > 0
    saga = await owner.fetchrow("SELECT exit_event_time, exit_camera_id FROM exit_saga WHERE id = $1", view.id)
    assert saga is not None and abs(saga["exit_event_time"] - seen_at) < timedelta(seconds=1), (
        "время выезда — когда камера увидела машину, а не когда кассир нажал"
    )
    row = await owner.fetchrow(
        "SELECT status::text AS status, closed_reason FROM parking_session WHERE id = $1", unknown.session_id
    )
    assert row is not None and row["status"] == "CANCELLED" and row["closed_reason"] == "EXIT_MATCHED"
    assert await container.occupancy.get(LOT_A1) == 4, "разбор больше не занимает место"
    assert await container.parking.advance_saga(TENANT_A, view.id) is SagaState.PAYMENT_REQUIRED
    assert await _audit(owner, unknown.session_id, "EXIT_MATCHED_MANUAL") == 1
    assert await _audit(owner, parked, "EXIT_STARTED_MANUAL") == 1
    with pytest.raises(ConflictError):
        await container.parking.match_exit(ADMIN, unknown.session_id, parked, reason="второй раз")


async def test_match_requires_exit_without_entry_and_parked_target(container: Container, clean_lot: None) -> None:
    a = await _enter(container, _plate())
    b = await _enter(container, _plate())
    with pytest.raises(ConflictError) as not_unknown:
        await container.parking.match_exit(ADMIN, a, b, reason="не разбор")
    assert not_unknown.value.code == "NOT_UNKNOWN_EXIT"
    unknown = await container.parking.start_exit(exit_cmd(_plate()))
    assert unknown.session_id is not None
    with pytest.raises(ConflictError) as itself:
        await container.parking.match_exit(ADMIN, unknown.session_id, unknown.session_id, reason="сама себя")
    assert itself.value.code == "MATCH_NOT_ACTIVE"


def test_plate_distance_reads_like_night_misreads() -> None:
    assert plate_distance("90D07CD", "90D077DD") <= 3  # потерян символ + спутана буква
    assert plate_distance("F2155BB", "90F215BB") <= 3  # обрезан код региона
    assert plate_distance("A0A557AA", "90A557WA") <= 3
    assert plate_distance("90O115XA", "90P115XA") == 1  # P/O — не путаница формы, одна правка
    assert plate_distance("90O102WA", "90O102WA") == 0
    assert plate_distance("90A123BC", "90D456XA") > 3  # другая машина
