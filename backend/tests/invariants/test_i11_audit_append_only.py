"""I-11. AuditLog доступен только на INSERT и SELECT.

Механизм: GRANT INSERT, SELECT на audit_log ролям приложения; UPDATE/DELETE/TRUNCATE не выданы (и явно отозваны).
При нарушении: следы ручных действий можно подчистить — аудируемость (CORE §2 п.4) теряется.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from parking.platform.db.tenant import tenant_transaction
from tests.conftest import TENANT_A, USER_A_ADMIN, expect_error


async def _insert(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        """
        INSERT INTO audit_log (tenant_id, actor_type, actor_id, user_id, action, entity_type, entity_id, reason, correlation_id)
        VALUES ($1, 'USER', $2, $2, 'BARRIER_OPEN', 'barrier', $3, 'тест I-11', $4) RETURNING id
        """,
        TENANT_A,
        USER_A_ADMIN,
        uuid4(),
        uuid4(),
    )


async def test_app_roles_cannot_update_or_delete_audit(
    critical: asyncpg.Connection, bulk: asyncpg.Connection, maintenance: asyncpg.Connection
) -> None:
    async with tenant_transaction(critical, TENANT_A):
        audit_id = await _insert(critical)
    for conn in (critical, bulk, maintenance):
        await expect_error(
            conn,
            TENANT_A,
            asyncpg.InsufficientPrivilegeError,
            "UPDATE audit_log SET reason = 'x' WHERE id = $1",
            audit_id,
        )
        await expect_error(
            conn, TENANT_A, asyncpg.InsufficientPrivilegeError, "DELETE FROM audit_log WHERE id = $1", audit_id
        )
        await expect_error(conn, TENANT_A, asyncpg.InsufficientPrivilegeError, "TRUNCATE audit_log")


async def test_user_action_requires_user_and_reason(critical: asyncpg.Connection) -> None:
    async with tenant_transaction(critical, TENANT_A):
        with pytest.raises(asyncpg.CheckViolationError):
            await critical.execute(
                """
                INSERT INTO audit_log (tenant_id, actor_type, user_id, action, entity_type, reason, correlation_id)
                VALUES ($1, 'USER', $2, 'BARRIER_OPEN', 'barrier', '', $3)
                """,
                TENANT_A,
                USER_A_ADMIN,
                uuid4(),
            )


@pytest.mark.parametrize("table", ["anpr_event", "manual_correction", "tariff_plan_version"])
async def test_other_append_only_tables(critical: asyncpg.Connection, table: str) -> None:
    grants = await critical.fetch(
        "SELECT privilege_type FROM information_schema.role_table_grants WHERE grantee = 'app_critical' AND table_name = $1",
        table,
    )
    assert {g["privilege_type"] for g in grants} == {"INSERT", "SELECT"}


async def test_tariff_version_rules_are_immutable_only_validity_is_updatable(critical: asyncpg.Connection) -> None:
    columns = await critical.fetch(
        "SELECT column_name FROM information_schema.column_privileges WHERE grantee = 'app_critical' AND table_name = 'tariff_plan_version' AND privilege_type = 'UPDATE'"
    )
    assert {c["column_name"] for c in columns} == {"validity"}
