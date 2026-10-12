"""Полные номера в панели — решение клиента: политика площадки show_full_plates (по умолчанию маска 01A***AA).
Включена — номер целиком видят сотрудники, работающие с машинами (кассир/оператор, менеджер, администратор,
владелец) в карточке, списке, поиске и карточке машины; наблюдателю — по-прежнему маска. Смена политики — с причиной,
в аудите. Хранение не меняется: открытого номера в БД нет (I-10), события живой ленты — с маской."""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from parking.shared.plate import mask
from tests.conftest import LOT_A1
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate

REASON = "владелец решил показывать кассирам номера целиком"


async def _policies(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, Any]:
    lots = await client.get("/api/v1/lots", headers=admin)
    assert lots.status_code == 200, lots.text
    return dict(next(lot for lot in lots.json() if lot["id"] == str(LOT_A1))["policies"])


async def _plate_as(client: httpx.AsyncClient, headers: dict[str, str], session_id: str) -> str:
    resp = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["plate"])


async def test_full_plates_are_shown_only_when_lot_allows_and_only_to_lot_staff(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection, entry_key: str
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    operator = await bearer(client, users[Role.OPERATOR])
    viewer = await bearer(client, users[Role.VIEWER])
    plate = unique_plate()
    entered = await send_event(client, entry_key, event_body(plate))
    assert entered.status_code == 200, entered.text
    session_id = str(entered.json()["session_id"])
    original = await _policies(client, admin)
    assert original["show_full_plates"] is False  # по умолчанию — маска
    assert await _plate_as(client, operator, session_id) == mask(plate)

    try:
        enabled = await client.patch(
            f"/api/v1/lots/{LOT_A1}",
            headers=admin,
            json={"policies": {**original, "show_full_plates": True}, "reason": REASON},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["policies"]["show_full_plates"] is True
        assert enabled.json()["policies"]["barrier_cidr_allowlist"] == original["barrier_cidr_allowlist"]

        assert await _plate_as(client, operator, session_id) == plate
        assert await _plate_as(client, admin, session_id) == plate
        assert await _plate_as(client, viewer, session_id) == mask(plate)  # наблюдатель — маска

        listed = await client.get(f"/api/v1/lots/{LOT_A1}/sessions", headers=operator, params={"status": "ACTIVE"})
        assert listed.status_code == 200, listed.text
        assert plate in {item["plate"] for item in listed.json()["items"]}
        found = await client.get("/api/v1/sessions/search", headers=operator, params={"plate": plate})
        assert [s["plate"] for s in found.json() if s["id"] == session_id] == [plate]
        card = await client.post("/api/v1/vehicles/lookup", headers=operator, json={"plate": plate})
        assert card.status_code == 200 and card.json()["plate"] == plate

        audit = await owner.fetchrow(
            "SELECT reason FROM audit_log WHERE action = 'LOT_UPDATED' AND entity_id = $1 ORDER BY created_at DESC LIMIT 1",
            LOT_A1,
        )
        assert audit is not None and audit["reason"] == REASON
    finally:
        restored = await client.patch(
            f"/api/v1/lots/{LOT_A1}", headers=admin, json={"policies": original, "reason": "вернуть политику площадки"}
        )
        assert restored.status_code == 200, restored.text

    assert await _plate_as(client, operator, session_id) == mask(plate)
