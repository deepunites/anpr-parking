"""I-16. Ручная коррекция номера не затирает исходное распознавание.

Механизм: исходное распознавание живёт в anpr_event (plate_hash/plate_encrypted, confidence) и в vehicle;
коррекция создаёт/находит другую vehicle, переключает parking_session.vehicle_id и пишет append-only строку
manual_correction (old_vehicle_id, new_vehicle_id, corrected_plate_encrypted, reason, user_id). anpr_event и
старая vehicle не изменяются (UPDATE/DELETE anpr_event запрещены роли приложения).
При нарушении: после «исправления» оператора невозможно доказать, что камера распознала на самом деле —
споры, невозможность разбора злоупотреблений.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import TENANT_A, USER_A_ADMIN
from tests.support.camera_fixtures import event_body, send_event, unique_plate

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())


async def test_correction_leaves_anpr_event_and_old_vehicle_untouched(
    client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection, clean_lot: None
) -> None:
    raw_plate = unique_plate("01")
    resp = await send_event(client, entry_key, event_body(raw_plate, confidence=91))
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    event_before = await owner.fetchrow(
        "SELECT id, plate_hash, confidence, camera_id, event_time FROM anpr_event WHERE session_id = $1", session_id
    )
    assert event_before is not None
    old_vehicle = await owner.fetchrow(
        "SELECT id, plate_hash, plate_encrypted FROM vehicle WHERE id = (SELECT vehicle_id FROM parking_session WHERE id = $1)",
        session_id,
    )
    assert old_vehicle is not None

    corrected_plate = unique_plate("77")
    view = await container.parking.correct_plate(
        ADMIN, session_id, PlateNumber.parse(corrected_plate), reason="оператор сверил по фото"
    )
    assert view.vehicle_id != old_vehicle["id"]

    event_after = await owner.fetchrow(
        "SELECT id, plate_hash, confidence, camera_id, event_time FROM anpr_event WHERE session_id = $1", session_id
    )
    assert event_after == event_before, "исходное распознавание не изменилось"
    assert (
        await owner.fetchrow("SELECT id, plate_hash, plate_encrypted FROM vehicle WHERE id = $1", old_vehicle["id"])
        == old_vehicle
    ), "исходная машина не изменена и не удалена"
    correction = await owner.fetchrow(
        "SELECT old_vehicle_id, new_vehicle_id, corrected_plate_encrypted FROM manual_correction WHERE session_id = $1",
        session_id,
    )
    assert correction is not None and correction["old_vehicle_id"] == old_vehicle["id"]
    assert correction["new_vehicle_id"] == view.vehicle_id and len(correction["corrected_plate_encrypted"]) > 16
    assert (
        container.plate_crypto.decrypt(TENANT_A, correction["corrected_plate_encrypted"], 1).value == corrected_plate
    ), "исправленный номер восстановим из append-only записи"
    # хеш исходного распознавания по-прежнему совпадает с камерным номером
    assert event_after["plate_hash"] == container.plate_crypto.hash(TENANT_A, PlateNumber.parse(raw_plate))


async def test_app_role_cannot_rewrite_recognition(owner: asyncpg.Connection, critical: asyncpg.Connection) -> None:
    from parking.platform.db.tenant import tenant_transaction

    event_id = await owner.fetchval("SELECT id FROM anpr_event LIMIT 1")
    if event_id is None:
        pytest.skip("нет событий")
    for sql in (
        "UPDATE anpr_event SET plate_hash = '\\x00' WHERE id = $1",
        "DELETE FROM anpr_event WHERE id = $1",
        "UPDATE manual_correction SET reason = 'x' WHERE session_id = $1",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with tenant_transaction(critical, TENANT_A):
                await critical.execute(sql, event_id)
