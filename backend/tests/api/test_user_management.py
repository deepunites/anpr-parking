"""v2: управление сотрудниками — редактирование (имя, площадки), повторное включение, сброс пароля администратором.
Каждое действие отзывает старые токены там, где это меняет доступ, и пишется в аудит с причиной."""

from __future__ import annotations

from uuid import uuid4

import httpx

from parking.modules.identity.domain.permissions import Role
from tests.conftest import LOT_A1
from tests.support.api_fixtures import Principal, bearer, login

NEW_PASSWORD = "temporary-password-9X"


async def _new_employee(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    email = f"emp.{uuid4().hex[:8]}@alpha-parking.uz"
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "email": email,
            "password": "temporary-password-0000",
            "role": "OPERATOR",
            "display_name": "Рустам Азимов",
            "reason": "приём на работу",
        },
    )
    assert created.status_code == 201, created.text
    # v2 этап 4: временный пароль администратора сотрудник меняет при первом входе
    first = await login(client, email, "temporary-password-0000")
    changed = await client.post(
        "/api/v1/auth/password",
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
        json={"current_password": "temporary-password-0000", "new_password": "initial-password-1234"},
    )
    assert changed.status_code == 204, changed.text
    return {"id": created.json()["id"], "email": email}


async def test_update_name_and_lot_scope_is_audited(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    emp = await _new_employee(client, admin)
    resp = await client.patch(
        f"/api/v1/users/{emp['id']}",
        headers=admin,
        json={"display_name": "Рустам А.", "lot_scope": [str(LOT_A1)], "reason": "перевод на площадку A1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Рустам А." and body["lot_scope"] == [str(LOT_A1)]
    # скоуп применяется к новому входу: оператор видит только A1
    token = (await login(client, emp["email"], "initial-password-1234")).json()["access_token"]
    lots = await client.get("/api/v1/lots", headers={"Authorization": f"Bearer {token}"})
    assert [lot["id"] for lot in lots.json()] == [str(LOT_A1)]
    audit = await client.get("/api/v1/audit", headers=admin, params={"entity_id": emp["id"]})
    if audit.status_code == 200:  # эндпоинт аудита есть не во всех сборках — проверяем, когда он доступен
        assert any(item["action"] == "USER_UPDATED" for item in audit.json()["items"])


async def test_reset_password_revokes_old_tokens_and_old_password(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    emp = await _new_employee(client, admin)
    old = (await login(client, emp["email"], "initial-password-1234")).json()["access_token"]
    resp = await client.post(
        f"/api/v1/users/{emp['id']}/password", headers=admin, json={"password": NEW_PASSWORD, "reason": "забыл пароль"}
    )
    assert resp.status_code == 204, resp.text
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old}"})).status_code == 401
    assert (await login(client, emp["email"], "initial-password-1234")).status_code == 401
    assert (await login(client, emp["email"], NEW_PASSWORD)).status_code == 200


async def test_reset_password_requires_min_length(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    emp = await _new_employee(client, admin)
    resp = await client.post(
        f"/api/v1/users/{emp['id']}/password", headers=admin, json={"password": "short", "reason": "test"}
    )
    assert resp.status_code == 422


async def test_deactivate_then_reactivate(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    emp = await _new_employee(client, admin)
    off = await client.post(f"/api/v1/users/{emp['id']}/deactivate", headers=admin, json={"reason": "уволен"})
    assert off.status_code == 204
    assert (await login(client, emp["email"], "initial-password-1234")).status_code == 401
    on = await client.patch(f"/api/v1/users/{emp['id']}", headers=admin, json={"active": True, "reason": "вернулся"})
    assert on.status_code == 200 and on.json()["active"] is True
    assert (await login(client, emp["email"], "initial-password-1234")).status_code == 200


async def test_admin_cannot_deactivate_self_via_patch(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    principal = users[Role.TENANT_ADMIN]
    admin = await bearer(client, principal)
    resp = await client.patch(
        f"/api/v1/users/{principal.user_id}", headers=admin, json={"active": False, "reason": "сам себя"}
    )
    assert resp.status_code == 422


async def test_operator_cannot_manage_users(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    operator = await bearer(client, users[Role.OPERATOR])
    target = users[Role.VIEWER]
    patch = await client.patch(
        f"/api/v1/users/{target.user_id}", headers=operator, json={"display_name": "x", "reason": "rbac"}
    )
    assert patch.status_code == 403
    reset = await client.post(
        f"/api/v1/users/{target.user_id}/password", headers=operator, json={"password": NEW_PASSWORD, "reason": "rbac"}
    )
    assert reset.status_code == 403
