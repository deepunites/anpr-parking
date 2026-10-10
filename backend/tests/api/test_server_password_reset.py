"""Сброс забытого пароля с консоли сервера площадки: deploy/local/stand/reset_password.py (запускает
deploy/truenas/reset-admin.sh внутри контейнера api). Тот же сервис сотрудников, что и кнопка в панели: старый пароль
и сессии перестают действовать, новый — временный, в аудите «консоль сервера»; счётчик неудачных входов обнуляется."""

from __future__ import annotations

import sys
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from parking.modules.identity.domain.permissions import Role
from parking.runtime.container import Container
from parking.shared.errors import NotFoundError
from tests.conftest import BACKEND
from tests.support.api_fixtures import Principal, bearer, login

sys.path.insert(0, str(BACKEND.parent / "deploy" / "local" / "stand"))
import reset_password

TEMPORARY = "server-reset-AbCd-2345"


async def test_console_reset_gives_temporary_password_and_lifts_lockout(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    email = f"site.admin.{uuid4().hex[:8]}@alpha-parking.uz"
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "email": email,
            "password": "temporary-password-0000",
            "role": "OPERATOR",
            "display_name": "Забыл пароль",
            "reason": "приём на работу",
        },
    )
    assert created.status_code == 201, created.text
    first = await login(client, email, "temporary-password-0000")
    changed = await client.post(
        "/api/v1/auth/password",
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
        json={"current_password": "temporary-password-0000", "new_password": "forgotten-password-1234"},
    )
    assert changed.status_code == 204, changed.text
    for _ in range(3):  # пароль не вспомнили — неудачные попытки копят блокировку
        assert (await login(client, email, "maybe-this-one-0000")).status_code == 401

    value = await reset_password.reset_login(container, owner, f"  {email.upper()} ", TEMPORARY)

    assert value == email
    assert await owner.fetchval("SELECT count(*) FROM login_attempt WHERE NOT success AND email_hash IS NOT NULL") == 0
    assert (await login(client, email, "forgotten-password-1234")).status_code == 401
    ok = await login(client, email, TEMPORARY)
    assert ok.status_code == 200, ok.text
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"})
    assert me.json()["must_change_password"] is True  # при входе — экран «задайте свой пароль»
    audit = await owner.fetchrow(
        "SELECT user_id, reason FROM audit_log WHERE action = 'PASSWORD_RESET_BY_ADMIN' AND entity_id = $1",
        UUID(created.json()["id"]),
    )
    assert audit is not None and audit["user_id"] == reset_password.SERVER_CONSOLE
    assert audit["reason"] == reset_password.REASON


async def test_console_reset_unknown_login_changes_nothing(container: Container, owner: asyncpg.Connection) -> None:
    with pytest.raises(NotFoundError):
        await reset_password.reset_login(container, owner, "nobody@alpha-parking.uz", TEMPORARY)
