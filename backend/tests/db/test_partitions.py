"""Управление партициями: создание вперёд, дроп по ретеншену, покрытие."""

from __future__ import annotations

import asyncpg
import pytest


async def test_ensure_is_idempotent_and_covers_ahead(owner: asyncpg.Connection) -> None:
    created = await owner.fetchval("SELECT app.ensure_range_partitions('outbox', 'day', 1, 10)")
    assert created >= 0
    again = await owner.fetchval("SELECT app.ensure_range_partitions('outbox', 'day', 1, 10)")
    assert again == 0
    coverage = await owner.fetchval("SELECT app.partition_coverage('outbox')")
    assert coverage.days >= 9


async def test_drop_old_partitions_only(owner: asyncpg.Connection) -> None:
    await owner.execute(
        "CREATE TABLE IF NOT EXISTS outbox_20200101 PARTITION OF outbox FOR VALUES FROM ('2020-01-01') TO ('2020-01-02')"
    )
    dropped = await owner.fetchval("SELECT app.drop_range_partitions('outbox', interval '7 days')")
    assert dropped == 1
    assert await owner.fetchval("SELECT to_regclass('outbox_20200101')") is None
    assert await owner.fetchval("SELECT app.partition_coverage('outbox')") is not None


async def test_maintenance_role_can_manage_partitions_but_not_critical(
    maintenance: asyncpg.Connection, critical: asyncpg.Connection
) -> None:
    assert await maintenance.fetchval("SELECT app.ensure_range_partitions('consumer_inbox', 'day', 1, 3)") >= 0
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await critical.fetchval("SELECT app.ensure_range_partitions('consumer_inbox', 'day', 1, 3)")


async def test_insert_outside_partitions_fails_loudly(owner: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="no partition"):
        await owner.execute(
            """
            INSERT INTO outbox (created_at, tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id)
            VALUES ('2000-01-01', '00000000-0000-4000-8000-0000000000a1', 'x', gen_random_uuid(), 'a.b.c.v1', '{}', gen_random_uuid())
            """
        )
