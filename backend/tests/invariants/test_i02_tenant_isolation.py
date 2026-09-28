"""I-2. Ни один запрос не возвращает и не пишет строки чужого tenant.

Механизм: (1) обязательный фильтр tenant_id в репозиториях; (2) RLS на каждой tenant-owned таблице для ролей
app_critical/app_bulk с USING/WITH CHECK tenant_id = app.current_tenant(); без SET LOCAL — ноль строк (fail-closed);
роли NOBYPASSRLS; партиции без прав — доступ только через родителя. Оба рубежа проверяются независимо.
При нарушении: чужие строки видны/записываются — фатально по CORE §2 п.3.
"""

from __future__ import annotations

import asyncpg
import pytest

from parking.modules.parking.infrastructure.session_reader import SessionReader
from parking.platform.db.tenant import tenant_transaction
from tests.conftest import (
    CAM_A1_IN,
    LOT_A1,
    LOT_B1,
    TENANT_A,
    TENANT_B,
    expect_error,
    new_session,
    new_vehicle,
    plate_hash,
)


async def test_every_tenant_table_has_rls_and_both_policies(owner: asyncpg.Connection) -> None:
    rows = await owner.fetch(
        """
        SELECT c.relname, c.relrowsecurity,
               (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname AND p.policyname LIKE 'rls_tenant_%') AS tenant_policies,
               (SELECT count(*) FROM pg_policies p WHERE p.tablename = c.relname AND p.policyname LIKE 'rls_maint_%') AS maint_policies
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
        """
    )
    assert rows, "нет ни одной таблицы с tenant_id?"
    bad = [
        r["relname"] for r in rows if not r["relrowsecurity"] or r["tenant_policies"] != 1 or r["maint_policies"] != 1
    ]
    assert bad == [], f"таблицы с tenant_id без полного RLS: {bad}"
    tenant_rls = await owner.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname = 'tenant'")
    assert tenant_rls is True
    roles = await owner.fetch("SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname LIKE 'app_%'")
    assert {r["rolname"] for r in roles} >= {"app_critical", "app_bulk", "app_maintenance"}
    assert all(not r["rolbypassrls"] and not r["rolsuper"] for r in roles)


async def test_without_tenant_context_nothing_is_visible_or_writable(critical: asyncpg.Connection) -> None:
    assert await critical.fetchval("SELECT count(*) FROM tenant") == 0
    assert await critical.fetchval("SELECT count(*) FROM parking_lot") == 0
    assert await critical.fetchval("SELECT count(*) FROM parking_session") == 0
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await critical.execute(
            "INSERT INTO parking_lot (tenant_id, code, name, capacity, timezone) VALUES ($1, 'X', 'x', 10, 'Asia/Tashkent')",
            TENANT_A,
        )


async def test_raw_sql_under_tenant_b_does_not_see_tenant_a(
    owner: asyncpg.Connection, critical: asyncpg.Connection
) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I02-A-CAR")
    session_id = await new_session(owner, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)
    async with tenant_transaction(critical, TENANT_B):
        assert await critical.fetchval("SELECT count(*) FROM parking_session WHERE id = $1", session_id) == 0
        assert await critical.fetchval("SELECT count(*) FROM vehicle WHERE id = $1", vehicle_id) == 0
        assert await critical.fetchval("SELECT count(*) FROM parking_lot WHERE id = $1", LOT_A1) == 0
        assert await critical.fetchval("SELECT code FROM tenant") == "beta"
    async with tenant_transaction(critical, TENANT_A):
        assert await critical.fetchval("SELECT count(*) FROM parking_session WHERE id = $1", session_id) == 1


async def test_insert_with_foreign_tenant_id_is_rejected(critical: asyncpg.Connection) -> None:
    async with tenant_transaction(critical, TENANT_B):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await critical.execute(
                "INSERT INTO parking_lot (tenant_id, code, name, capacity, timezone) VALUES ($1, 'EVIL', 'x', 10, 'Asia/Tashkent')",
                TENANT_A,
            )


async def test_update_cannot_move_row_to_another_tenant(
    owner: asyncpg.Connection, critical: asyncpg.Connection
) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I02-MOVE")
    async with tenant_transaction(critical, TENANT_A):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await critical.execute("UPDATE vehicle SET tenant_id = $1 WHERE id = $2", TENANT_B, vehicle_id)


async def test_direct_partition_access_is_denied(critical: asyncpg.Connection) -> None:
    await expect_error(
        critical, TENANT_A, asyncpg.InsufficientPrivilegeError, "SELECT * FROM parking_session_p0 LIMIT 1"
    )
    partition = await critical.fetchval(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'anpr_event'::regclass LIMIT 1"
    )
    await expect_error(critical, TENANT_A, asyncpg.InsufficientPrivilegeError, f"SELECT * FROM {partition} LIMIT 1")


async def test_repository_filters_tenant_independently_of_rls(
    owner: asyncpg.Connection, critical: asyncpg.Connection
) -> None:
    vehicle_id = await new_vehicle(owner, TENANT_A, "I02-REPO")
    await new_session(owner, TENANT_A, LOT_A1, vehicle_id, CAM_A1_IN)

    # 1) репозиторий под владельцем схемы (RLS не действует) — фильтр tenant_id работает сам по себе
    reader_owner = SessionReader(owner)
    assert await reader_owner.find_active(TENANT_A, LOT_A1, plate_hash("I02-REPO")) is not None
    assert await reader_owner.find_active(TENANT_B, LOT_A1, plate_hash("I02-REPO")) is None
    assert await reader_owner.list_by_lot(TENANT_B, LOT_A1) == []

    # 2) RLS под ролью приложения с чужим tenant — даже запрос без фильтра пуст
    async with tenant_transaction(critical, TENANT_B):
        assert await critical.fetchval("SELECT count(*) FROM parking_session WHERE lot_id = $1", LOT_A1) == 0
        assert await SessionReader(critical).list_by_lot(TENANT_A, LOT_A1) == []

    # 3) оба рубежа вместе — свой tenant видит свою сессию
    async with tenant_transaction(critical, TENANT_A):
        found = await SessionReader(critical).find_active(TENANT_A, LOT_A1, plate_hash("I02-REPO"))
        assert found is not None and found.tenant_id == TENANT_A


async def test_maintenance_role_sees_all_tenants_without_bypassrls(maintenance: asyncpg.Connection) -> None:
    assert await maintenance.fetchval("SELECT count(*) FROM tenant") == 2
    assert await maintenance.fetchval("SELECT count(DISTINCT tenant_id) FROM parking_lot") == 2


async def test_cross_tenant_fk_is_physically_impossible(owner: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await owner.execute(
            "INSERT INTO camera (tenant_id, lot_id, code, name, direction) VALUES ($1, $2, 'EVIL', 'x', 'IN')",
            TENANT_B,
            LOT_A1,
        )
    assert LOT_B1 != LOT_A1
