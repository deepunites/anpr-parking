"""Ручные действия оператора: коррекция номера (I-16), закрытие, orphan, подтверждение въезда — всё с reason и аудитом (I-15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import EntryStatus
from parking.runtime.container import Container
from parking.shared.errors import ConflictError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer
from tests.support.parking_fixtures import entry_cmd

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())


async def test_plate_correction_keeps_original_recognition(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    wrong = PlateNumber.parse("01A123AA")
    entry = await container.parking.process_entry(entry_cmd(wrong))
    assert entry.session_id is not None
    old_vehicle = await owner.fetchval("SELECT vehicle_id FROM parking_session WHERE id = $1", entry.session_id)
    view = await container.parking.correct_plate(
        ADMIN, entry.session_id, PlateNumber.parse("01A128AA"), reason="камера перепутала 3 и 8"
    )
    assert view.vehicle_id != old_vehicle and view.plate == "01A***AA"
    correction = await owner.fetchrow(
        "SELECT old_vehicle_id, new_vehicle_id, reason, user_id FROM manual_correction WHERE session_id = $1",
        entry.session_id,
    )
    assert (
        correction is not None
        and correction["old_vehicle_id"] == old_vehicle
        and correction["new_vehicle_id"] == view.vehicle_id
    )
    assert correction["user_id"] == USER_A_ADMIN and "3 и 8" in correction["reason"]
    assert await owner.fetchval("SELECT count(*) FROM vehicle WHERE id = $1", old_vehicle) == 1, (
        "исходная машина не удалена"
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'PLATE_CORRECTED' AND session_id = $1", entry.session_id
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.plate.corrected.v1' AND aggregate_id = $1",
            entry.session_id,
        )
        == 1
    )
    # коррекция на номер, у которого уже есть ACTIVE-сессия на площадке — конфликт I-1
    other = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01A777AA")))
    assert other.session_id is not None
    with pytest.raises(ConflictError):
        await container.parking.correct_plate(ADMIN, other.session_id, PlateNumber.parse("01A128AA"), reason="дубль")


async def test_manual_close_orphan_and_confirm(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    review = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01M111MM"), requires_review=True))
    assert review.status is EntryStatus.MANUAL_REVIEW and review.session_id is not None
    confirmed = await container.parking.confirm_entry(ADMIN, review.session_id, reason="номер сверен по фото")
    assert confirmed.status.value == "ACTIVE"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM barrier_command WHERE session_id = $1 AND source = 'MANUAL'", review.session_id
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'BARRIER_OPEN_MANUAL' AND session_id = $1", review.session_id
        )
        == 1
    )
    assert await container.occupancy.get(LOT_A1) == 1

    closed = await container.parking.close_manual(ADMIN, review.session_id, reason="стрела поднята ключом")
    assert (
        closed.status.value == "CLOSED"
        and closed.exit_kind is not None
        and closed.exit_kind.value == "MANUAL"
        and closed.billing_minutes is not None
    )
    assert await container.occupancy.get(LOT_A1) == 0

    stale = await container.parking.process_entry(
        entry_cmd(PlateNumber.parse("01M222MM"), at=datetime.now(tz=UTC) - timedelta(days=4))
    )
    assert stale.session_id is not None
    swept = await container.parking.sweep_stale(TENANT_A)
    assert (
        swept >= 1
        and await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", stale.session_id) == "ORPHAN"
    )
    fresh = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01M333MM")))
    assert fresh.session_id is not None
    orphaned = await container.parking.mark_orphan(ADMIN, fresh.session_id, reason="уехал за эвакуатором")
    assert orphaned.status.value == "ORPHAN"


async def test_session_endpoints(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], clean_lot: None
) -> None:
    operator = await bearer(client, users[Role.OPERATOR])
    entry = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01Q444QQ")))
    listing = await client.get(f"/api/v1/lots/{LOT_A1}/sessions", headers=operator, params={"status": ["ACTIVE"]})
    assert listing.status_code == 200, listing.text
    ids = [s["id"] for s in listing.json()["items"]]
    assert str(entry.session_id) in ids and listing.json()["items"][0]["plate"] == "01Q***QQ"
    occupancy = await client.get(f"/api/v1/lots/{LOT_A1}/occupancy", headers=operator)
    assert occupancy.status_code == 200 and occupancy.json()["occupied"] == 1 and occupancy.json()["capacity"] == 800
    search = await client.get("/api/v1/sessions/search", headers=operator, params={"plate": "01 Q 444 QQ"})
    assert [s["id"] for s in search.json()] == [str(entry.session_id)]
    corrected = await client.post(
        f"/api/v1/sessions/{entry.session_id}/correct-plate",
        headers=operator,
        json={"plate": "01Q445QQ", "reason": "опечатка камеры"},
    )
    assert corrected.status_code == 200 and corrected.json()["plate"] == "01Q***QQ"
    no_reason = await client.post(f"/api/v1/sessions/{entry.session_id}/close", headers=operator, json={})
    assert no_reason.status_code == 422
    closed = await client.post(
        f"/api/v1/sessions/{entry.session_id}/close", headers=operator, json={"reason": "выехала через ручной режим"}
    )
    assert closed.status_code == 200 and closed.json()["status"] == "CLOSED"
    viewer = await bearer(client, users[Role.VIEWER])
    assert (
        await client.post(f"/api/v1/sessions/{entry.session_id}/orphan", headers=viewer, json={"reason": "x"})
    ).status_code == 403
