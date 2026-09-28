"""I-12. Периоды действия версий одного тарифного плана не пересекаются.

Механизм: EXCLUDE USING gist (plan_id WITH =, validity WITH &&) на непартиционированной tariff_plan_version (btree_gist).
При нарушении: для момента выезда подходят две версии — расчёт недетерминирован (ломает и I-7).
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import PLAN_A1, TENANT_A, USER_A_ADMIN

RULES = '{"grace_minutes": 15, "first_hour_price": 500000, "price_per_hour": 300000, "rounding_mode": "UP", "rounding_step_minutes": 60}'


async def _add_version(owner: asyncpg.Connection, version: int, validity: str) -> None:
    await owner.execute(
        """
        INSERT INTO tariff_plan_version (tenant_id, plan_id, version, rules, validity, created_by)
        VALUES ($1, $2, $3, $4::jsonb, $5::text::tstzrange, $6)
        """,
        TENANT_A,
        PLAN_A1,
        version,
        RULES,
        validity,
        USER_A_ADMIN,
    )


async def test_overlapping_version_rejected(owner: asyncpg.Connection) -> None:
    # сид: версия 1 действует с 2026-01-01 без верхней границы
    with pytest.raises(asyncpg.ExclusionViolationError, match="ex_tpv_no_overlap"):
        await _add_version(owner, 2, "[2026-10-01 00:00+05,)")


async def test_adjacent_versions_allowed_after_closing_previous(owner: asyncpg.Connection) -> None:
    tr = owner.transaction()
    await tr.start()
    try:
        await owner.execute(
            "UPDATE tariff_plan_version SET validity = tstzrange(lower(validity), '2026-10-01 00:00+05', '[)') WHERE plan_id = $1 AND version = 1",
            PLAN_A1,
        )
        await _add_version(owner, 2, "[2026-10-01 00:00+05,)")
        assert await owner.fetchval("SELECT count(*) FROM tariff_plan_version WHERE plan_id = $1", PLAN_A1) == 2
        with pytest.raises(asyncpg.ExclusionViolationError):
            await _add_version(owner, 3, "[2026-09-15 00:00+05,2026-10-15 00:00+05)")
    finally:
        await tr.rollback()


async def test_rules_shape_is_checked(owner: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="tariff_plan_version_rules_check"):
        await owner.execute(
            """
            INSERT INTO tariff_plan_version (tenant_id, plan_id, version, rules, validity, created_by)
            VALUES ($1, $2, 9, '{"price_per_hour": 12.5}'::jsonb, '[2030-01-01,)'::tstzrange, $3)
            """,
            TENANT_A,
            PLAN_A1,
            USER_A_ADMIN,
        )
