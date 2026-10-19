"""Второе чтение номера (MediaPark, 2026-10-19): регион не попадает в кадр камеры въезда («Q475UAUA», «00Q708MA»),
одну машину камера читает дважды за секунды (70Z525CA и 90Z525CA). Повторное чтение не создаёт вторую сессию и не
открывает стрелу; полный номер исправляет неполный. Камера выезда, видящая въезжающую машину сзади (эхо въезда),
«дописывает» регион. Две разные машины подряд с похожими номерами не склеиваются."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import EntryStatus, ExitStatus
from parking.modules.parking.domain.similar import is_complete, same_car_reads
from parking.modules.tenancy.application.contracts import LotPolicies
from parking.modules.tenancy.application.lot_service import UpdateLotCommand
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.parking_fixtures import entry_cmd, exit_cmd

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
LETTERS = "ABEHKMNPTXYZ"


def _tail() -> str:
    """Номер без региона: буква, три цифры, две буквы (Q475UA)."""
    n = uuid4().int
    return f"{LETTERS[n % 12]}{100 + n // 12 % 900}{LETTERS[n // 10800 % 12]}{LETTERS[n // 129600 % 12]}"


async def _plate_of(owner: asyncpg.Connection, container: Container, session_id: UUID) -> str:
    row = await owner.fetchrow(
        "SELECT v.plate_encrypted, v.enc_key_version FROM parking_session s JOIN vehicle v ON v.id = s.vehicle_id "
        "WHERE s.id = $1",
        session_id,
    )
    assert row is not None
    return container.plate_crypto.decrypt(TENANT_A, bytes(row["plate_encrypted"]), row["enc_key_version"]).value


async def _active(owner: asyncpg.Connection, since: datetime) -> int:
    return int(
        await owner.fetchval(
            "SELECT count(*) FROM parking_session WHERE lot_id = $1 AND status = 'ACTIVE' AND entry_time >= $2",
            LOT_A1,
            since,
        )
    )


def test_complete_and_same_car_reads() -> None:
    assert is_complete("90Z525CA") and is_complete("01123ABC")
    assert not is_complete("Q475UAUA") and not is_complete("00Q708MA") and not is_complete("M0M079UA")
    assert same_car_reads("70Z525CA", "90Z525CA"), "отличается только регион"
    assert same_car_reads("Q475UAUA", "90Q475UA"), "регион обрезан кадром"
    assert not same_car_reads("90A123BC", "90A124BC"), "две разные машины подряд"


async def test_double_read_with_other_region_keeps_one_session(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    tail = _tail()
    at = datetime.now(tz=UTC) - timedelta(minutes=5)
    first = await container.parking.process_entry(entry_cmd(PlateNumber.parse(f"70{tail}"), at=at))
    second = await container.parking.process_entry(
        entry_cmd(PlateNumber.parse(f"90{tail}"), at=at + timedelta(seconds=4))
    )

    assert first.status is EntryStatus.ENTRY_CONFIRMED and first.session_id is not None
    assert second.status is EntryStatus.ENTRY_DOUBLE_READ and second.session_id == first.session_id
    assert second.barrier_command_id is None, "стрела второй раз не открывается"
    assert await _active(owner, at) == 1 and await container.occupancy.get(LOT_A1) == 1


async def test_complete_second_read_corrects_cut_region(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    tail = _tail()
    at = datetime.now(tz=UTC) - timedelta(minutes=5)
    first = await container.parking.process_entry(entry_cmd(PlateNumber.parse(f"{tail}{tail[-2:]}"), at=at))
    assert first.session_id is not None
    second = await container.parking.process_entry(
        entry_cmd(PlateNumber.parse(f"90{tail}"), at=at + timedelta(seconds=3))
    )

    assert second.status is EntryStatus.ENTRY_DOUBLE_READ and second.session_id == first.session_id
    assert await _plate_of(owner, container, first.session_id) == f"90{tail}", "полный номер исправил неполный"
    audit = await owner.fetchval(
        "SELECT count(*) FROM audit_log WHERE session_id = $1 AND action = 'PLATE_CORRECTED_BY_CAMERA' "
        "AND actor_type = 'CAMERA'",
        first.session_id,
    )
    assert audit == 1


async def test_different_cars_one_after_another_are_not_merged(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    tail = _tail()
    other = tail[:1] + str((int(tail[1:4]) + 1) % 900 + 100) + tail[4:]
    at = datetime.now(tz=UTC) - timedelta(minutes=5)
    a = await container.parking.process_entry(entry_cmd(PlateNumber.parse(f"90{tail}"), at=at))
    b = await container.parking.process_entry(entry_cmd(PlateNumber.parse(f"90{other}"), at=at + timedelta(seconds=5)))
    assert a.status is EntryStatus.ENTRY_CONFIRMED and b.status is EntryStatus.ENTRY_CONFIRMED
    assert a.session_id != b.session_id and await _active(owner, at) == 2


async def test_exit_camera_completes_the_region_of_entering_car(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    await container.lots.update(
        ADMIN, LOT_A1, UpdateLotCommand(policies=LotPolicies(exit_min_stay_seconds=60)), reason="эхо въезда"
    )
    try:
        tail = _tail()
        at = datetime.now(tz=UTC) - timedelta(minutes=5)
        entered = await container.parking.process_entry(entry_cmd(PlateNumber.parse(f"{tail}{tail[-2:]}"), at=at))
        assert entered.session_id is not None
        full = PlateNumber.parse(f"90{tail}")

        echo = await container.parking.start_exit(exit_cmd(full, at=at + timedelta(seconds=9)))

        assert echo.status is ExitStatus.EXIT_ENTRY_ECHO and echo.session_id == entered.session_id
        assert await _plate_of(owner, container, entered.session_id) == full.value, "камера выезда дописала регион"
        real = await container.parking.start_exit(exit_cmd(full, at=at + timedelta(minutes=4)))
        assert real.session_id == entered.session_id and real.saga_id is not None, "настоящий выезд находит машину"
    finally:
        await container.lots.update(ADMIN, LOT_A1, UpdateLotCommand(policies=LotPolicies()), reason="cleanup")
