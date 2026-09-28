"""ADR-02: роли и права. Критичные таблицы недоступны на запись из app_bulk; app_critical не правит версии тарифов."""

from __future__ import annotations

import asyncpg

from parking.platform.db.tenant import tenant_transaction
from tests.conftest import CAM_A1_IN, TENANT_A, expect_error


async def test_role_level_synchronous_commit(owner: asyncpg.Connection) -> None:
    rows = await owner.fetch(
        "SELECT r.rolname, s.setconfig FROM pg_roles r JOIN pg_db_role_setting s ON s.setrole = r.oid WHERE r.rolname LIKE 'app_%'"
    )
    settings = {r["rolname"]: set(r["setconfig"]) for r in rows}
    assert "synchronous_commit=remote_apply" in settings["app_critical"]
    assert "synchronous_commit=off" in settings["app_bulk"]
    assert "synchronous_commit=on" in settings["app_maintenance"]


async def test_bulk_cannot_write_critical_tables(bulk: asyncpg.Connection) -> None:
    statements: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO payment (tenant_id, lot_id, session_id, amount, method, calculation_snapshot, idempotency_key)"
            " VALUES ($1, $1, $1, 0, 'GRACE', '{}', sha256('x'::bytea))",
            (TENANT_A,),
        ),
        ("UPDATE parking_session SET status = 'CLOSED'", ()),
        (
            "INSERT INTO outbox (tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id)"
            " VALUES ($1, 'x', $1, 'a.b.c.v1', '{}', $1)",
            (TENANT_A,),
        ),
        ("UPDATE camera SET name = 'x'", ()),
    ]
    for stmt, args in statements:
        await expect_error(bulk, TENANT_A, asyncpg.InsufficientPrivilegeError, stmt, *args)


async def test_bulk_can_flush_camera_status_columns(bulk: asyncpg.Connection) -> None:
    async with tenant_transaction(bulk, TENANT_A):
        await bulk.execute("UPDATE camera SET status = 'ONLINE', last_heartbeat_at = now() WHERE id = $1", CAM_A1_IN)
        assert await bulk.fetchval("SELECT status::text FROM camera WHERE id = $1", CAM_A1_IN) == "ONLINE"


async def test_critical_cannot_change_tariff_version_or_delete_sessions(critical: asyncpg.Connection) -> None:
    for stmt in (
        "UPDATE tariff_plan_version SET version = version",
        "DELETE FROM parking_session",
        "DELETE FROM outbox",
    ):
        await expect_error(critical, TENANT_A, asyncpg.InsufficientPrivilegeError, stmt)


async def test_pre_tenant_lookups_work_only_through_functions(critical: asyncpg.Connection) -> None:
    assert await critical.fetchval("SELECT count(*) FROM app_user") == 0
    row = await critical.fetchrow("SELECT * FROM app.lookup_user_by_email('admin@alpha-parking.uz')")
    assert row is not None and row["tenant_id"] == TENANT_A
    keys = await critical.fetch("SELECT * FROM app.lookup_camera_keys('devkey_entry_01a')")
    assert len(keys) == 1 and keys[0]["camera_id"] == CAM_A1_IN
    assert await critical.fetch("SELECT * FROM app.lookup_camera_keys('nope_nope_nope__')") == []
