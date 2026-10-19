"""VIP бесплатно (MediaPark, 2026-10-19: «VIP считает как всем»): машина стоит 8 часов — обычная заплатила бы, VIP
выезжает через камеру бесплатно и стрела открывается сама; и когда VIP добавили после въезда (решение «обычная»
уже в кешах процесса и Redis — добавление правила кеши сбрасывает), и когда до."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from parking.modules.identity.domain.permissions import Role
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate
from tests.support.parking_fixtures import entry_cmd


async def _vip(client: httpx.AsyncClient, admin: dict[str, str], plate: str) -> None:
    added = await client.post(
        "/api/v1/access-rules", headers=admin, json={"plate": plate, "kind": "VIP", "reason": "директор"}
    )
    assert added.status_code == 201, added.text


async def _exit(client: httpx.AsyncClient, exit_key: str, plate: str) -> httpx.Response:
    exited = await send_event(client, exit_key, event_body(plate, event_type="EXIT", camera_code="EXIT_01"))
    assert exited.status_code == 200, exited.text
    return exited


async def test_normal_car_pays_but_vip_added_after_entry_exits_free(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], exit_key: str
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    hours_ago = datetime.now(tz=UTC) - timedelta(hours=8)
    normal = unique_plate()
    await container.parking.process_entry(entry_cmd(PlateNumber.parse(normal), at=hours_ago))
    assert (await _exit(client, exit_key, normal)).json()["status"] == "PAYMENT_REQUIRED", "контроль: обычная платит"

    vip = unique_plate()
    entered = await container.parking.process_entry(entry_cmd(PlateNumber.parse(vip), at=hours_ago))
    assert entered.session_id is not None
    quote = await client.get(f"/api/v1/sessions/{entered.session_id}/quote", headers=admin)
    assert quote.status_code == 200 and quote.json()["amount_minor"] > 0, "пока обычная — к оплате (и в кешах)"
    await _vip(client, admin, vip)

    assert (await _exit(client, exit_key, vip)).json()["status"] == "EXIT_CONFIRMED", "VIP — без оплаты"


async def test_vip_before_entry_exits_free(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], exit_key: str
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    vip = unique_plate()
    await _vip(client, admin, vip)
    await container.parking.process_entry(
        entry_cmd(PlateNumber.parse(vip), at=datetime.now(tz=UTC) - timedelta(hours=8))
    )
    assert (await _exit(client, exit_key, vip)).json()["status"] == "EXIT_CONFIRMED"
