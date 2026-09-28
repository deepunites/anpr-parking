"""Tenant-контекст транзакции для RLS (фаза 2 §1.3).

Правило: `SET LOCAL` внутри транзакции — безопасно под PgBouncer transaction pooling; `SET` вне транзакции — нет.
Здесь используется `set_config(..., is_local => true)`, что эквивалентно SET LOCAL, но допускает bind-параметр.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg

TENANT_SETTING = "app.current_tenant_id"


@asynccontextmanager
async def tenant_transaction(conn: asyncpg.Connection, tenant_id: UUID) -> AsyncIterator[asyncpg.Connection]:
    """Открывает транзакцию и выставляет tenant для политик RLS до конца транзакции.

    Бросает asyncpg-исключения транзакции как есть. После выхода переменная сбрасывается вместе с транзакцией.
    """
    async with conn.transaction():
        await conn.execute("SELECT set_config($1, $2, true)", TENANT_SETTING, str(tenant_id))
        yield conn


async def current_tenant(conn: asyncpg.Connection) -> UUID | None:
    """Возвращает tenant текущей транзакции или None, если контекст не установлен."""
    value = await conn.fetchval("SELECT app.current_tenant()")
    return None if value is None else UUID(str(value))
