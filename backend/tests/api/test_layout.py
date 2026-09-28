"""v2 этап 5 «Карта»: схема площадки версиями (конфликт правок, проверка документа и устройств, места → parking_spot)
и живая карта: машина, въехавшая через камеру зоны, увеличивает занятость этой зоны; ручной въезд — «вне зон»."""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from tests.conftest import CAM_A1_IN, LOT_A1
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate

URL = f"/api/v1/lots/{LOT_A1}"


def _document(prefix: str = "A") -> dict[str, Any]:
    return {
        "width": 1200,
        "height": 800,
        "grid": 10,
        "zones": [
            {"id": "z1", "name": "P1 — первый этаж", "color": "blue", "x": 20, "y": 20, "w": 600, "h": 400},
            {"id": "z2", "name": "VIP", "color": "amber", "x": 650, "y": 20, "w": 300, "h": 400, "capacity": 4},
        ],
        "rows": [
            {"id": "r1", "zone_id": "z1", "x": 40, "y": 40, "count": 12, "direction": "h", "prefix": prefix},
            {
                "id": "r2",
                "zone_id": "z1",
                "x": 40,
                "y": 200,
                "count": 12,
                "direction": "h",
                "prefix": prefix,
                "start": 13,
            },
            {"id": "r3", "zone_id": "z2", "x": 670, "y": 40, "count": 6, "direction": "v", "prefix": "V"},
        ],
        "roads": [{"id": "road", "x": 20, "y": 440, "w": 930, "h": 60, "label": "Проезд"}],
        "devices": [{"id": "cam-in", "kind": "camera", "ref_id": str(CAM_A1_IN), "x": 100, "y": 100}],
        "labels": [{"id": "gate", "kind": "entry", "x": 30, "y": 470, "text": "Въезд"}],
    }


async def _save(client: httpx.AsyncClient, headers: dict[str, str], document: dict[str, Any]) -> httpx.Response:
    current = (await client.get(f"{URL}/layout", headers=headers)).json()["version"]
    return await client.put(
        f"{URL}/layout", headers=headers, json={"base_version": current, "document": document, "reason": "схема"}
    )


async def test_layout_versions_conflict_and_spot_sync(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    saved = await _save(client, admin, _document())
    assert saved.status_code == 200, saved.text
    body = saved.json()
    version = body["version"]
    assert body["spots"] == 30
    zones = {z["id"]: z for z in body["zones"]}
    assert zones["z1"]["capacity"] == 24 and zones["z2"]["capacity"] == 4 and zones["z2"]["spots"] == 6

    stale = await client.put(
        f"{URL}/layout", headers=admin, json={"base_version": version - 1, "document": _document()}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "LAYOUT_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == version

    spots = await owner.fetch("SELECT code, zone, x, y, active FROM parking_spot WHERE lot_id = $1", LOT_A1)
    active = {r["code"]: r for r in spots if r["active"]}
    assert {"A1", "A24", "V1", "V6"} <= set(active)
    assert active["A13"]["zone"] == "P1 — первый этаж" and active["A13"]["y"] == 200
    assert active["V2"]["x"] == 670 and active["V2"]["y"] == 90  # вертикальный ряд: шаг — высота места

    # другой префикс: прежние места не удаляются, а выключаются (история и ссылки целы)
    renamed = await _save(client, admin, _document(prefix="B"))
    assert renamed.status_code == 200 and renamed.json()["version"] == version + 1
    codes = {
        r["code"]: r["active"]
        for r in await owner.fetch("SELECT code, active FROM parking_spot WHERE lot_id = $1", LOT_A1)
    }
    assert codes["B1"] is True and codes["A1"] is False

    history = (await client.get(f"{URL}/layout/versions", headers=admin)).json()
    assert [h["version"] for h in history[:2]] == [version + 1, version]
    old = await client.get(f"{URL}/layout/versions/{version}", headers=admin)
    assert old.status_code == 200 and old.json()["document"]["rows"][0]["prefix"] == "A"


async def test_layout_document_is_validated(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    dup = _document()
    dup["rows"][1]["start"] = 5  # A5..A16 пересекаются с A1..A12
    bad = await _save(client, admin, dup)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_LAYOUT"
    assert "A5" in bad.json()["error"]["message"]

    ghost = _document()
    ghost["devices"].append(
        {"id": "x", "kind": "camera", "ref_id": "00000000-0000-4000-8000-00000000dead", "x": 1, "y": 1}
    )
    unknown = await _save(client, admin, ghost)
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "UNKNOWN_DEVICE"

    wrong_zone = _document()
    wrong_zone["rows"][0]["zone_id"] = "nope"
    assert (await _save(client, admin, wrong_zone)).status_code == 422

    operator = await bearer(client, users[Role.OPERATOR])
    assert (await _save(client, operator, _document())).status_code == 403  # рисует администратор


async def test_live_map_counts_entries_by_camera_zone(
    client: httpx.AsyncClient, users: dict[Role, Principal], entry_key: str
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    assert (await _save(client, admin, _document())).status_code == 200
    operator = await bearer(client, users[Role.OPERATOR])

    async def load() -> dict[str, Any]:
        resp = await client.get(f"{URL}/map", headers=operator)
        assert resp.status_code == 200, resp.text
        return resp.json()

    before = await load()
    z1_before = next(z for z in before["zones"] if z["id"] == "z1")["occupied"]
    assert any(d["kind"] == "camera" and d["zone_id"] == "z1" for d in before["devices"])  # зона — по положению значка

    entered = await send_event(client, entry_key, event_body(unique_plate()))
    assert entered.status_code == 200, entered.text
    manual = await client.post(
        f"{URL}/manual-entry", headers=admin, json={"plate": unique_plate("10"), "reason": "камера не распознала"}
    )
    assert manual.status_code == 201, manual.text

    after = await load()
    assert next(z for z in after["zones"] if z["id"] == "z1")["occupied"] == z1_before + 1
    assert after["unzoned_occupied"] == before["unzoned_occupied"] + 1
    assert after["occupied"] == before["occupied"] + 2
    assert next(z for z in after["zones"] if z["id"] == "z2")["occupied"] == 0
