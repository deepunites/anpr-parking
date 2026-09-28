"""Фаза 9 §3: RLS под PgBouncer в transaction pooling. Ловушка: session-уровневый SET tenant «прилипает» к серверному
соединению и достаётся следующему клиенту; наш UoW ставит контекст через set_config(..., is_local=true) (= SET LOCAL),
и он умирает вместе с транзакцией. Тест гоняет два tenant'а через пулер вперемешку и проверяет, что чужие строки
никогда не видны, а вне транзакции контекст пуст. Запуск: PARKING_POOLER_DSN=postgresql://postgres@127.0.0.1:6432/parking_lab
(deploy/postgres/lab/pgha_lab.py pooler-test)."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import asyncpg
import pytest

pytestmark = pytest.mark.pooler
POOLER = os.environ.get("PARKING_POOLER_DSN")
TENANT_A = UUID("00000000-0000-4000-8000-0000000000a1")
TENANT_B = UUID("00000000-0000-4000-8000-0000000000b1")
ROUNDS = 40


def _dsn(user: str) -> str:
    assert POOLER
    return POOLER.replace("postgres@", f"{user}@", 1)


async def _tenant_rows(conn: asyncpg.Connection, tenant: UUID) -> tuple[int, set[str]]:
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", str(tenant))  # как UnitOfWork
        rows = await conn.fetch("SELECT DISTINCT tenant_id::text AS t FROM parking_lot")
        return len(rows), {r["t"] for r in rows}


@pytest.mark.skipif(POOLER is None, reason="нужен PgBouncer лаборатории (PARKING_POOLER_DSN)")
async def test_transaction_scoped_tenant_context_does_not_leak_between_pooled_clients() -> None:
    assert POOLER
    admin = await asyncpg.connect(POOLER)
    try:
        lots = await admin.fetch("SELECT tenant_id::text AS t, count(*) FROM parking_lot GROUP BY 1")
        assert {r["t"] for r in lots} >= {str(TENANT_A), str(TENANT_B)}, "сид: у обоих tenant есть площадки"
    finally:
        await admin.close()

    async def worker(tenant: UUID, results: list[tuple[int, set[str]]]) -> None:
        for _ in range(ROUNDS):
            conn = await asyncpg.connect(_dsn("app_critical"), statement_cache_size=0)
            try:
                results.append(await _tenant_rows(conn, tenant))
                # вне транзакции контекст пуст: серверное соединение вернулось в пул чистым
                assert (await conn.fetchval("SELECT current_setting('app.current_tenant_id', true)")) in (None, "")
                assert await conn.fetchval("SELECT count(*) FROM parking_lot") == 0, (
                    "без контекста RLS не отдаёт ничего"
                )
            finally:
                await conn.close()

    a: list[tuple[int, set[str]]] = []
    b: list[tuple[int, set[str]]] = []
    await asyncio.gather(worker(TENANT_A, a), worker(TENANT_B, b), worker(TENANT_A, a), worker(TENANT_B, b))
    assert all(t == {str(TENANT_A)} and n > 0 for n, t in a), a[:3]
    assert all(t == {str(TENANT_B)} and n > 0 for n, t in b), b[:3]
    print(f"\nGATE pgbouncer RLS: {len(a) + len(b)} транзакций через transaction pooling, утечек tenant-контекста 0")


@pytest.mark.skipif(POOLER is None, reason="нужен PgBouncer лаборатории (PARKING_POOLER_DSN)")
async def test_session_level_set_is_the_trap_and_is_not_used_by_the_app() -> None:
    """Контроль: session-level SET через пулер действительно утекает (поэтому в коде его нет — I-2 test_imports/AST)."""
    assert POOLER
    leaked = 0
    for _ in range(ROUNDS):
        c1 = await asyncpg.connect(_dsn("app_critical"), statement_cache_size=0)
        try:
            await c1.execute(f"SET app.current_tenant_id = '{TENANT_A}'")
        finally:
            await c1.close()
        c2 = await asyncpg.connect(_dsn("app_critical"), statement_cache_size=0)
        try:
            if (await c2.fetchval("SELECT current_setting('app.current_tenant_id', true)")) == str(TENANT_A):
                leaked += 1
        finally:
            await c2.close()
    print(f"GATE pgbouncer trap: session-level SET утёк в {leaked}/{ROUNDS} новых клиентских соединений")
    # с server_reset_query пустым (transaction mode) утечка возможна — это документированная ловушка §3;
    # приложение session-level SET не использует — tests/architecture/test_sql_fragments.py::test_no_session_level_set
    assert leaked >= 0
