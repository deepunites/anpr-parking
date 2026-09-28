"""v2: карточка машины (марка/модель/цвет) и ручная регистрация въезда.

Номер — только в теле запроса; в ответах маскирован; марка видна в сессии и в правилах доступа."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from parking.modules.identity.domain.permissions import Role
from tests.conftest import LOT_A1
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import unique_plate


def _uz_plate() -> str:
    """Уникальный номер в формате UZ_STANDARD: 01A123AA (цифры и буквы из uuid)."""
    raw = unique_plate("")  # 6 hex-символов
    digits = "".join(str(int(c, 16) % 10) for c in raw[:3])
    letters = "".join("ABCEHKMOPTXY"[int(c, 16) % 12] for c in raw[3:6])
    return f"01{letters[0]}{digits}{letters[1:]}"


async def test_set_and_lookup_vehicle_attributes(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    headers = await bearer(client, users[Role.OPERATOR])
    plate = _uz_plate()
    missing = await client.post("/api/v1/vehicles/lookup", headers=headers, json={"plate": plate})
    assert missing.status_code == 200 and missing.json()["known"] is False
    saved = await client.post(
        "/api/v1/vehicles",
        headers=headers,
        json={
            "plate": plate.lower().replace("a", "а"),
            "make": " Chevrolet ",
            "model": "Cobalt",
            "color": "белый",
            "reason": "уточнил у водителя",
        },
    )  # кириллица и регистр нормализуются так же, как у камеры
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["make"] == "Chevrolet" and body["model"] == "Cobalt" and body["color"] == "белый"
    assert plate not in saved.text  # номер в ответе маскирован
    looked = await client.post("/api/v1/vehicles/lookup", headers=headers, json={"plate": plate})
    assert looked.json()["known"] is True and looked.json()["make"] == "Chevrolet"


async def test_manual_entry_creates_active_session_with_make(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    headers = await bearer(client, users[Role.OPERATOR])
    plate = _uz_plate()
    entry_time = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
    resp = await client.post(
        f"/api/v1/lots/{LOT_A1}/manual-entry",
        headers=headers,
        json={
            "plate": plate,
            "entry_time": entry_time,
            "make": "Lada",
            "model": "Vesta",
            "color": "серый",
            "reason": "камера не распознала грязный номер",
        },
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()
    assert session["status"] == "ACTIVE" and session["entry_manual"] is True
    assert session["vehicle_make"] == "Lada" and session["entry_camera_id"] is None
    got = await client.get(f"/api/v1/sessions/{session['id']}", headers=headers)
    assert got.json()["vehicle_model"] == "Vesta"
    # второй ручной въезд той же машины — 409, а не вторая активная сессия (I-1)
    again = await client.post(
        f"/api/v1/lots/{LOT_A1}/manual-entry", headers=headers, json={"plate": plate, "reason": "повтор"}
    )
    assert again.status_code == 409
    lookup = await client.post("/api/v1/vehicles/lookup", headers=headers, json={"plate": plate})
    assert lookup.json()["active_session_id"] == session["id"]
    # марка из карточки сессии — без номера
    patched = await client.post(
        f"/api/v1/sessions/{session['id']}/vehicle",
        headers=headers,
        json={"make": "Lada", "model": "Granta", "color": "white", "reason": "видно на кадре"},
    )
    assert patched.status_code == 200 and patched.json()["vehicle_model"] == "Granta"


async def test_manual_entry_of_blacklisted_car_goes_to_review(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    manager = await bearer(client, users[Role.MANAGER])
    plate = _uz_plate()
    rule = await client.post(
        "/api/v1/access-rules", headers=manager, json={"plate": plate, "kind": "BLACKLIST", "reason": "угон"}
    )
    assert rule.status_code == 201, rule.text
    resp = await client.post(
        f"/api/v1/lots/{LOT_A1}/manual-entry", headers=manager, json={"plate": plate, "reason": "охрана впустила"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "MANUAL_REVIEW" and resp.json()["review_reason"] == "ENTRY_BLACKLIST"


async def test_manual_entry_validation(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    headers = await bearer(client, users[Role.OPERATOR])
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = await client.post(
        f"/api/v1/lots/{LOT_A1}/manual-entry",
        headers=headers,
        json={"plate": _uz_plate(), "entry_time": future, "reason": "тест"},
    )
    assert resp.status_code == 422
    bad = await client.post(
        f"/api/v1/lots/{LOT_A1}/manual-entry", headers=headers, json={"plate": "!!", "reason": "тест"}
    )
    assert bad.status_code == 422
    no_reason = await client.post(f"/api/v1/lots/{LOT_A1}/manual-entry", headers=headers, json={"plate": _uz_plate()})
    assert no_reason.status_code == 422


async def test_viewer_cannot_register_entry_or_edit_vehicle(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    headers = await bearer(client, users[Role.VIEWER])
    plate = _uz_plate()
    assert (
        await client.post(
            f"/api/v1/lots/{LOT_A1}/manual-entry", headers=headers, json={"plate": plate, "reason": "x-x"}
        )
    ).status_code == 403
    assert (
        await client.post("/api/v1/vehicles", headers=headers, json={"plate": plate, "make": "BMW", "reason": "x-x"})
    ).status_code == 403


async def test_access_rule_with_make_shows_make_in_list(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    manager = await bearer(client, users[Role.MANAGER])
    plate = _uz_plate()
    created = await client.post(
        "/api/v1/access-rules",
        headers=manager,
        json={
            "plate": plate,
            "kind": "WHITELIST",
            "discount_percent": 50,
            "make": "Toyota",
            "model": "Camry",
            "color": "чёрный",
            "note": "директор",
            "reason": "служебная машина",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["vehicle_make"] == "Toyota"
    listing = await client.get(
        "/api/v1/access-rules", headers=manager, params={"kind": "WHITELIST", "discount_percent": 50, "limit": 200}
    )
    row = next(r for r in listing.json()["items"] if r["id"] == created.json()["id"])
    assert row["vehicle_model"] == "Camry" and row["vehicle_color"] == "чёрный"
