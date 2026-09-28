"""v2 этап 4 «Сотрудники»: вход по телефону, временный пароль со сменой при первом входе, приглашения по ссылке,
защита ролей платформы от администратора клиента, завершение сессий при сбросе пароля."""

from __future__ import annotations

import secrets
from uuid import uuid4

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from parking.runtime.container import Container
from tests.conftest import LOT_A1, LOT_A2, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer, ensure_user, login

TEMP = "temporary-password-12"
NEW = "my-own-password-2026"


def _phone() -> tuple[str, str]:
    """(как вводит человек, как хранится)"""
    digits = f"9{secrets.randbelow(10)}{secrets.randbelow(10_000_000):07d}"
    return f"+998 {digits[:2]} {digits[2:5]}-{digits[5:7]}-{digits[7:]}", f"+998{digits}"


def _err(resp: httpx.Response) -> str:
    return str(resp.json()["error"]["code"])


async def _login(client: httpx.AsyncClient, ident: str, password: str) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"login": ident, "password": password})


def _auth(resp: httpx.Response) -> dict[str, str]:
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_phone_login_and_forced_password_change(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    typed, stored = _phone()
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={"phone": typed, "password": TEMP, "role": "OPERATOR", "display_name": "Шерзод", "reason": "приём"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["phone"] == stored and created.json()["email"] is None
    assert created.json()["must_change_password"] is True

    # один и тот же номер в разной записи — это тот же логин
    dup = await client.post(
        "/api/v1/users",
        headers=admin,
        json={"phone": stored[4:], "password": TEMP, "role": "OPERATOR", "display_name": "Дубль", "reason": "приём"},
    )
    assert dup.status_code == 409 and _err(dup) == "PHONE_TAKEN"

    emp = _auth(await _login(client, stored[4:], TEMP))  # местный формат без +998
    me = (await client.get("/api/v1/auth/me", headers=emp)).json()
    assert me["must_change_password"] is True and me["phone"] == stored
    blocked = await client.get("/api/v1/lots", headers=emp)
    assert blocked.status_code == 403 and _err(blocked) == "PASSWORD_CHANGE_REQUIRED"

    wrong = await client.post(
        "/api/v1/auth/password", headers=emp, json={"current_password": "nope", "new_password": NEW}
    )
    assert wrong.status_code == 422 and _err(wrong) == "WRONG_PASSWORD"
    same = await client.post(
        "/api/v1/auth/password", headers=emp, json={"current_password": TEMP, "new_password": TEMP}
    )
    assert same.status_code == 422 and _err(same) == "PASSWORD_UNCHANGED"
    ok = await client.post("/api/v1/auth/password", headers=emp, json={"current_password": TEMP, "new_password": NEW})
    assert ok.status_code == 204, ok.text
    assert (await client.get("/api/v1/auth/me", headers=emp)).status_code == 401  # старый токен отозван

    assert (await _login(client, typed, TEMP)).status_code == 401
    fresh = _auth(await _login(client, typed, NEW))
    assert (await client.get("/api/v1/lots", headers=fresh)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=fresh)).json()["must_change_password"] is False


async def test_admin_reset_ends_sessions_and_requires_change(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    email = f"reset.{uuid4().hex[:8]}@alpha-parking.uz"
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={"email": email, "password": TEMP, "role": "CASHIER", "display_name": "Кассир", "reason": "приём"},
    )
    uid = created.json()["id"]
    first = await _login(client, email, TEMP)
    emp = _auth(first)
    await client.post("/api/v1/auth/password", headers=emp, json={"current_password": TEMP, "new_password": NEW})

    session = await login(client, email, NEW)  # в cookie клиента — refresh этой сессии
    assert session.status_code == 200
    reset = await client.post(
        f"/api/v1/users/{uid}/password", headers=admin, json={"password": "admin-given-pass-1", "reason": "забыл"}
    )
    assert reset.status_code == 204
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401  # украденный refresh больше не работает
    again = _auth(await _login(client, email, "admin-given-pass-1"))
    assert (await client.get("/api/v1/auth/me", headers=again)).json()["must_change_password"] is True


async def test_invite_link_flow(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    typed, stored = _phone()
    resp = await client.post(
        "/api/v1/invites",
        headers=admin,
        json={"display_name": "Дилноза", "role": "CASHIER", "phone": typed, "reason": "новая смена"},
    )
    assert resp.status_code == 201, resp.text
    token, invite = resp.json()["token"], resp.json()["invite"]
    assert invite["status"] == "PENDING" and invite["phone"] == stored

    listed = await client.get("/api/v1/invites", headers=admin)
    assert any(i["id"] == invite["id"] for i in listed.json())

    preview = await client.get(f"/api/v1/join/{token}")
    assert preview.status_code == 200
    assert preview.json()["tenant_name"] == "Alpha Parking" and preview.json()["role"] == "CASHIER"
    assert preview.json()["login"] == stored

    short = await client.post(f"/api/v1/join/{token}", json={"password": "short"})
    assert short.status_code == 422
    accepted = await client.post(f"/api/v1/join/{token}", json={"password": NEW, "display_name": "Дилноза Р."})
    assert accepted.status_code == 200 and accepted.json()["login"] == stored

    emp = _auth(await _login(client, stored, NEW))
    me = (await client.get("/api/v1/auth/me", headers=emp)).json()
    assert me["role"] == "CASHIER" and me["must_change_password"] is False and me["display_name"] == "Дилноза Р."

    reuse = await client.post(f"/api/v1/join/{token}", json={"password": NEW})
    assert reuse.status_code == 409 and _err(reuse) == "INVITE_ACCEPTED"
    assert (await client.get(f"/api/v1/join/{token}")).status_code == 409
    assert (await client.get("/api/v1/join/" + "x" * 43)).status_code == 404


async def test_invite_revoke_expiry_and_taken_login(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    taken = await client.post(
        "/api/v1/invites",
        headers=admin,
        json={"display_name": "X", "role": "VIEWER", "email": users[Role.OPERATOR].email, "reason": "проверка"},
    )
    assert taken.status_code == 409 and _err(taken) == "EMAIL_TAKEN"
    none = await client.post(
        "/api/v1/invites", headers=admin, json={"display_name": "X", "role": "VIEWER", "reason": "проверка"}
    )
    assert none.status_code == 422 and _err(none) == "LOGIN_REQUIRED"

    async def invite() -> tuple[str, str]:
        r = await client.post(
            "/api/v1/invites",
            headers=admin,
            json={
                "display_name": "Y",
                "role": "OPERATOR",
                "email": f"inv.{uuid4().hex[:8]}@alpha-parking.uz",
                "reason": "проверка",
            },
        )
        return r.json()["token"], r.json()["invite"]["id"]

    token, invite_id = await invite()
    revoke = await client.post(f"/api/v1/invites/{invite_id}/revoke", headers=admin, json={"reason": "ошибся"})
    assert revoke.status_code == 204
    gone = await client.post(f"/api/v1/join/{token}", json={"password": NEW})
    assert gone.status_code == 409 and _err(gone) == "INVITE_REVOKED"

    token, invite_id = await invite()
    await owner.execute(
        "UPDATE user_invite SET expires_at = now() - interval '1 minute' WHERE id = $1::uuid", invite_id
    )
    late = await client.post(f"/api/v1/join/{token}", json={"password": NEW})
    assert late.status_code == 409 and _err(late) == "INVITE_EXPIRED"
    listed = {i["id"]: i["status"] for i in (await client.get("/api/v1/invites", headers=admin)).json()}
    assert listed[invite_id] == "EXPIRED"


async def test_tenant_admin_cannot_touch_platform_roles(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    """Администратор клиента не может выдать роль платформы и не может сбросить пароль владельцу платформы,
    заведённому в его клиенте, — иначе получил бы доступ ко всем клиентам платформы."""
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    body = {"email": f"esc.{uuid4().hex[:8]}@alpha-parking.uz", "password": TEMP, "display_name": "E", "reason": "x-x"}
    for role in ("PLATFORM_OWNER", "PLATFORM_ADMIN"):
        created = await client.post("/api/v1/users", headers=admin, json={**body, "role": role})
        assert created.status_code == 403 and _err(created) == "ROLE_NOT_GRANTABLE", role
        invited = await client.post(
            "/api/v1/invites",
            headers=admin,
            json={"display_name": "E", "role": role, "email": body["email"], "reason": "x-x"},
        )
        assert invited.status_code == 403, role
    owner_id = str(users[Role.PLATFORM_OWNER].user_id)
    reset = await client.post(
        f"/api/v1/users/{owner_id}/password", headers=admin, json={"password": "takeover-password", "reason": "x-x"}
    )
    assert reset.status_code == 403 and _err(reset) == "ROLE_NOT_GRANTABLE"
    demote = await client.patch(
        f"/api/v1/users/{owner_id}/role", headers=admin, json={"role": "VIEWER", "reason": "x-x"}
    )
    assert demote.status_code == 403
    off = await client.post(f"/api/v1/users/{owner_id}/deactivate", headers=admin, json={"reason": "x-x"})
    assert off.status_code == 403


async def test_scoped_admin_cannot_grant_lots_beyond_own(client: httpx.AsyncClient, container: Container) -> None:
    """v2 этап 8 (THREAT_MODEL T-26): администратор одной площадки не приглашает и не создаёт сотрудников на все
    площадки или на чужую площадку клиента."""
    admin = await ensure_user(
        container, TENANT_A, USER_A_ADMIN, Role.TENANT_ADMIN, "role.scoped_admin@alpha-parking.uz", frozenset({LOT_A1})
    )
    headers = await bearer(client, admin)

    def body(lots: list[str]) -> dict[str, object]:
        return {
            "display_name": "Кассир",
            "role": "CASHIER",
            "email": f"scoped.{uuid4().hex[:8]}@alpha-parking.uz",
            "lot_scope": lots,
            "reason": "новый кассир",
        }

    for lots in ([], [str(LOT_A2)], [str(LOT_A1), str(LOT_A2)]):
        resp = await client.post("/api/v1/invites", headers=headers, json=body(lots))
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "LOTS_OUT_OF_SCOPE", lots
    ok = await client.post("/api/v1/invites", headers=headers, json=body([str(LOT_A1)]))
    assert ok.status_code == 201, ok.text
    direct = await client.post(
        "/api/v1/users",
        headers=headers,
        json={**body([]), "password": "correct-horse-battery-staple", "reason": "новый кассир"},
    )
    assert direct.status_code == 403
