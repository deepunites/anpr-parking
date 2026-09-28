"""Изоляция tenant на уровне API (404, не 403) и скоуп площадок оператора (применяется в репозитории)."""

from __future__ import annotations

import httpx

from parking.modules.identity.domain.permissions import Role
from tests.conftest import LOT_A1, LOT_A2, LOT_B1
from tests.support.api_fixtures import Principal, bearer


async def test_tenant_b_cannot_see_tenant_a_objects(
    client: httpx.AsyncClient, tenant_b_admin: Principal, users: dict[Role, Principal]
) -> None:
    headers_b = await bearer(client, tenant_b_admin)
    lots = await client.get("/api/v1/lots", headers=headers_b)
    assert lots.status_code == 200 and {lot["id"] for lot in lots.json()} == {str(LOT_B1)}
    assert (await client.get(f"/api/v1/lots/{LOT_A1}", headers=headers_b)).status_code == 404
    patch = await client.patch(
        f"/api/v1/lots/{LOT_A1}", headers=headers_b, json={"name": "hacked", "reason": "cross-tenant"}
    )
    assert patch.status_code == 404
    user_a = users[Role.VIEWER]
    assert (await client.get(f"/api/v1/users/{user_a.user_id}", headers=headers_b)).status_code == 404
    role = await client.patch(
        f"/api/v1/users/{user_a.user_id}/role",
        headers=headers_b,
        json={"role": "TENANT_ADMIN", "reason": "cross-tenant"},
    )
    assert role.status_code == 404
    listing = await client.get("/api/v1/users", headers=headers_b)
    assert all(u["email"].endswith("@beta-garage.uz") for u in listing.json()["items"])


async def test_operator_scope_limits_lots(client: httpx.AsyncClient, scoped_operator: Principal) -> None:
    headers = await bearer(client, scoped_operator)
    lots = await client.get("/api/v1/lots", headers=headers)
    assert [lot["id"] for lot in lots.json()] == [str(LOT_A1)]
    assert (await client.get(f"/api/v1/lots/{LOT_A2}", headers=headers)).status_code == 404
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["lot_scope"] == [str(LOT_A1)]


async def test_role_change_takes_effect_immediately(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "email": "promote-me@alpha-parking.uz",
            "password": "temporary-password-1234",
            "role": "VIEWER",
            "display_name": "P",
            "reason": "test",
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    first = await client.post(
        "/api/v1/auth/login", json={"email": "promote-me@alpha-parking.uz", "password": "temporary-password-1234"}
    )
    changed_pwd = await client.post(  # v2 этап 4: временный пароль администратора меняется при первом входе
        "/api/v1/auth/password",
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
        json={"current_password": "temporary-password-1234", "new_password": "correct-horse-battery-staple"},
    )
    assert changed_pwd.status_code == 204
    login = await client.post(
        "/api/v1/auth/login", json={"email": "promote-me@alpha-parking.uz", "password": "correct-horse-battery-staple"}
    )
    old_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/users", headers=old_headers)).status_code == 403
    changed = await client.patch(
        f"/api/v1/users/{user_id}/role", headers=admin, json={"role": "MANAGER", "reason": "promotion"}
    )
    assert changed.status_code == 200 and changed.json()["role"] == "MANAGER"
    assert (await client.get("/api/v1/users", headers=old_headers)).status_code == 401, (
        "старый токен отвергнут немедленно"
    )
    relogin = await client.post(
        "/api/v1/auth/login", json={"email": "promote-me@alpha-parking.uz", "password": "correct-horse-battery-staple"}
    )
    new_headers = {"Authorization": f"Bearer {relogin.json()['access_token']}"}
    assert (await client.get("/api/v1/users", headers=new_headers)).status_code == 200
    deactivated = await client.post(f"/api/v1/users/{user_id}/deactivate", headers=admin, json={"reason": "cleanup"})
    assert deactivated.status_code == 204
    assert (await client.get("/api/v1/users", headers=new_headers)).status_code == 401


async def test_manual_actions_require_reason(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    response = await client.patch(f"/api/v1/lots/{LOT_A1}", headers=admin, json={"name": "x"})
    assert response.status_code == 422
    assert any("reason" in e["loc"] for e in response.json()["error"]["details"]["errors"])
