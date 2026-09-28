"""v2: «Проверить связь» со шлагбаумом из «Устройств» — без движения стрелы, с аудитом; ошибка связи — ответ 200
с ok=false и причиной (это результат проверки, а не сбой API)."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from tests.conftest import BARRIER_A1_IN
from tests.support.api_fixtures import Principal, bearer


async def test_probe_reports_unreachable_controller_and_is_audited(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    resp = await client.post(
        f"/api/v1/barriers/{BARRIER_A1_IN}/probe", headers=admin, json={"reason": "проверка после монтажа"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False and body["error"]  # сид: 10.10.1.20:502 — контроллера нет
    audited = await owner.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'BARRIER_PROBED' AND entity_id = $1", BARRIER_A1_IN
    )
    assert audited >= 1


async def test_probe_needs_reason_and_permission(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    operator = await bearer(client, users[Role.OPERATOR])
    denied = await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/probe", headers=operator, json={"reason": "x-x-x"})
    assert denied.status_code == 403
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    assert (await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/probe", headers=admin, json={})).status_code == 422
    missing = await client.post(f"/api/v1/barriers/{uuid4()}/probe", headers=admin, json={"reason": "проверка"})
    assert missing.status_code == 404


async def test_manual_open_command_status_is_visible(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    opened = await client.post(
        f"/api/v1/barriers/{BARRIER_A1_IN}/open", headers=admin, json={"reason": "проверка после монтажа"}
    )
    assert opened.status_code == 202, opened.text
    status = await client.get(f"/api/v1/barrier-commands/{opened.json()['command_id']}", headers=admin)
    assert status.status_code == 200
    assert status.json()["state"] in {"PENDING", "SENT", "OK", "FAILED", "SKIPPED"}
    tenant_b = await client.get(f"/api/v1/barrier-commands/{uuid4()}", headers=admin)
    assert tenant_b.status_code == 404
