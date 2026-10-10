"""Новый временный пароль сотруднику, который забыл свой (администратор площадки — тоже).

Запуск — с консоли сервера, внутри контейнера api с окружением стенда (права на консоль = права root, как и у
резервных копий): deploy/truenas/reset-admin.sh [логин]. Сброс идёт через сервис сотрудников приложения, как кнопка
«Сбросить пароль» в панели: старый пароль и все сессии сотрудника перестают действовать, при входе система попросит
задать свой пароль, в журнале аудита — PASSWORD_RESET_BY_ADMIN от «консоли сервера». Счётчик неудачных входов по
этому логину обнуляется, чтобы новый пароль сработал сразу.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from uuid import UUID

import asyncpg
from standlib import load_secrets, new_password, superuser_dsn

from parking.modules.identity.application.auth_service import email_hash
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.identity.domain.contact import normalize_email, normalize_phone
from parking.platform.config import load_settings
from parking.platform.db.engine import build_database
from parking.runtime.container import build_container
from parking.shared.errors import DomainError, NotFoundError

SERVER_CONSOLE = UUID("00000000-0000-4000-8000-00000000fffe")  # «кто сбросил» в журнале аудита
REASON = "сброс забытого пароля с консоли сервера"


def _login_key(login: str) -> tuple[str, str]:
    """('email', адрес) или ('phone', +998…): логин — как на экране входа."""
    raw = login.strip()
    if "@" in raw:
        return "email", normalize_email(raw)
    return "phone", normalize_phone(raw)


async def reset_login(container: Any, conn: asyncpg.Connection, login: str, password: str) -> str:
    """Временный пароль сотруднику по логину; conn — суперпользователь БД (поиск по всем клиентам, мимо RLS).
    Возвращает нормализованный логин."""
    kind, value = _login_key(login)
    query = (
        "SELECT id, tenant_id FROM app_user WHERE lower(email) = $1"
        if kind == "email"
        else "SELECT id, tenant_id FROM app_user WHERE phone = $1"
    )
    row = await conn.fetchrow(query, value)
    if row is None:
        raise NotFoundError(f"no user with login {value}")
    actor = CurrentUser(
        user_id=SERVER_CONSOLE, tenant_id=row["tenant_id"], role=Role.PLATFORM_ADMIN, lot_scope=frozenset()
    )
    await container.users.reset_password(actor, row["id"], password=password, reason=REASON)
    await conn.execute(
        "DELETE FROM login_attempt WHERE email_hash = ANY($1::bytea[])", [email_hash(login), email_hash(value)]
    )
    return value


async def main(login: str) -> None:
    settings = load_settings()  # окружение PARKING_* уже подставил entry.py
    database = build_database(settings)
    container = build_container(settings, database)
    conn = await asyncpg.connect(superuser_dsn(load_secrets()))
    try:
        password = new_password()
        value = await reset_login(container, conn, login, password)
    finally:
        await conn.close()
        await container.aclose()
        await database.dispose()
    print(f"\n  Логин:              {value}\n  Временный пароль:   {password}\n")
    print("  При входе система попросит задать свой пароль. Закройте окно консоли (или clear), когда перепишете.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        raise SystemExit("usage: reset_password.py <e-mail или телефон>")
    try:
        asyncio.run(main(sys.argv[1]))
    except DomainError as exc:  # нет такого логина, неверный формат телефона/e-mail
        raise SystemExit(f"[!] {exc.message}") from None
