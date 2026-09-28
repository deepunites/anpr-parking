"""Аутентификация: неразличимость ответов, ротация refresh, детекция reuse, logout, антибрутфорс."""

from __future__ import annotations

import statistics
import time

import asyncpg
import httpx
import pytest

from parking.modules.identity.domain.permissions import Role
from tests.conftest import TENANT_A
from tests.support.api_fixtures import PASSWORD, Principal, login


async def test_login_returns_access_token_and_refresh_cookie(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    response = await login(client, users[Role.OPERATOR].email)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 900 and body["access_token"].count(".") == 2
    cookie = response.headers["set-cookie"]
    assert (
        "parking_refresh=" in cookie
        and "HttpOnly" in cookie
        and "SameSite=strict" in cookie.lower().replace("samesite=strict", "SameSite=strict")
    )
    assert "Path=/api/v1/auth" in cookie
    assert "refresh_token" not in body


async def test_wrong_password_and_unknown_user_are_indistinguishable(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    wrong = await login(client, users[Role.VIEWER].email, "definitely-not-the-password")
    unknown = await login(client, "nobody@alpha-parking.uz", "definitely-not-the-password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "UNAUTHENTICATED"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"] == "invalid credentials"


async def test_timing_of_unknown_user_matches_wrong_password(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    async def timed(email: str) -> float:
        started = time.perf_counter()
        await login(client, email, "x" * 20)
        return time.perf_counter() - started

    known = [await timed(users[Role.VIEWER].email) for _ in range(4)]
    unknown = [await timed(f"ghost{i}@alpha-parking.uz") for i in range(4)]
    ratio = statistics.median(unknown) / statistics.median(known)
    assert 0.5 <= ratio <= 2.0, f"ratio={ratio:.2f} known={known} unknown={unknown}"
    assert statistics.median(unknown) > 0.05, "argon2 обязан выполняться и для несуществующего пользователя"


async def test_refresh_rotates_and_reuse_revokes_family(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    first = await login(client, users[Role.CASHIER].email)
    assert first.status_code == 200
    refresh_1 = first.cookies["parking_refresh"]

    second = await client.post("/api/v1/auth/refresh", headers={"Cookie": f"parking_refresh={refresh_1}"})
    assert second.status_code == 200, second.text
    refresh_2 = second.cookies["parking_refresh"]
    assert refresh_2 != refresh_1

    reuse = await client.post("/api/v1/auth/refresh", headers={"Cookie": f"parking_refresh={refresh_1}"})
    assert reuse.status_code == 401

    after_reuse = await client.post("/api/v1/auth/refresh", headers={"Cookie": f"parking_refresh={refresh_2}"})
    assert after_reuse.status_code == 401, "всё семейство отозвано после reuse"

    audit = await owner.fetchval(
        "SELECT count(*) FROM audit_log WHERE tenant_id = $1 AND action = 'REFRESH_REUSE_DETECTED'", TENANT_A
    )
    assert audit >= 1
    event = await owner.fetchval("SELECT count(*) FROM outbox WHERE event_type = 'identity.refresh.reuse_detected.v1'")
    assert event >= 1


async def test_logout_revokes_family_and_clears_cookie(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    logged = await login(client, users[Role.MANAGER].email)
    refresh = logged.cookies["parking_refresh"]
    out = await client.post("/api/v1/auth/logout", headers={"Cookie": f"parking_refresh={refresh}"})
    assert out.status_code == 204
    assert 'parking_refresh=""' in out.headers["set-cookie"] or "Max-Age=0" in out.headers["set-cookie"]
    assert (
        await client.post("/api/v1/auth/refresh", headers={"Cookie": f"parking_refresh={refresh}"})
    ).status_code == 401


async def test_me_reflects_role_permissions(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    token = (await login(client, users[Role.CASHIER].email)).json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert (
        body["role"] == "CASHIER"
        and "payment:create_cash" in body["permissions"]
        and "users:write" not in body["permissions"]
    )


async def test_tampered_and_none_alg_tokens_rejected(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    import jwt as pyjwt

    token = (await login(client, users[Role.VIEWER].email)).json()["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    claims["role"] = "PLATFORM_ADMIN"
    none_token = pyjwt.encode(claims, key="", algorithm="none")
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {none_token}"})).status_code == 401
    hs_token = pyjwt.encode(claims, key="public-key-as-hmac-secret", algorithm="HS256")
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {hs_token}"})).status_code == 401
    head, payload, sig = token.split(".")
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {head}.{payload}.{sig[:-3]}abc"})
    ).status_code == 401
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_bruteforce_throttling_and_lockout(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    email = users[Role.VIEWER].email
    await owner.execute("DELETE FROM login_attempt")
    statuses = [(await login(client, email, "wrong-password-!")).status_code for _ in range(3)]
    assert statuses == [401, 401, 401]
    throttled = await login(client, email, "wrong-password-!")
    assert throttled.status_code == 429 and throttled.headers["Retry-After"] == "1"
    assert throttled.json()["error"]["code"] == "RATE_LIMITED"
    # даже с верным паролем — пока действует задержка
    assert (await login(client, email, PASSWORD)).status_code == 429
    await owner.execute("DELETE FROM login_attempt")
    # блокировка после 10 неудач: считаем через failed_logins, обходя троттлинг по IP очисткой попыток
    await owner.execute("UPDATE app_user SET failed_logins = 9 WHERE email = $1", email)
    locked = await login(client, email, "wrong-password-!")
    assert locked.status_code == 401
    row = await owner.fetchrow("SELECT failed_logins, locked_until FROM app_user WHERE email = $1", email)
    assert row is not None and row["failed_logins"] == 10 and row["locked_until"] is not None
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'LOGIN_LOCKED' AND tenant_id = $1", TENANT_A
        )
        >= 1
    )
    await owner.execute("DELETE FROM login_attempt")
    assert (await login(client, email, PASSWORD)).status_code == 401, (
        "заблокированный пользователь неотличим от неверного пароля"
    )
    await owner.execute("UPDATE app_user SET failed_logins = 0, locked_until = NULL WHERE email = $1", email)
    await owner.execute("DELETE FROM login_attempt")
    assert (await login(client, email, PASSWORD)).status_code == 200


@pytest.mark.parametrize("payload", [{"password": "x"}, {"login": "a@b.c"}, {}])  # v2: login — e-mail или телефон
async def test_login_validation_errors_have_uniform_shape(client: httpx.AsyncClient, payload: dict[str, str]) -> None:
    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR" and "correlation_id" in body
    assert response.headers["x-correlation-id"] == body["correlation_id"]
