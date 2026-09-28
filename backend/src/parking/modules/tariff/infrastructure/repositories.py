from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.tariff.application.contracts import TariffPlanView, TariffVersionView
from parking.modules.tariff.application.ports import VersionRecord
from parking.shared.errors import ConflictError
from parking.shared.ttl_cache import TtlCache

TariffCache = TtlCache[tuple[UUID, UUID], VersionRecord]
"""Кеш процесса (tenant_id, lot_id) → действующая версия тарифа; запись годится, пока `covers(at)`."""

_VERSION_AT = text(
    """
    SELECT v.id, v.plan_id, p.lot_id, v.version, v.rules, v.holiday_calendar_id,
           lower(v.validity) AS valid_from, upper(v.validity) AS valid_to,
           coalesce((SELECT array_agg(h.day ORDER BY h.day) FROM holiday h WHERE h.calendar_id = v.holiday_calendar_id),
                    CAST('{}' AS date[])) AS holidays
    FROM tariff_plan p
    JOIN tariff_plan_version v ON v.plan_id = p.id
    WHERE p.lot_id = :lot AND p.is_default AND p.active AND v.validity @> CAST(:at AS timestamptz)
    """
)


class SqlTariffRepository:
    def __init__(
        self, session: AsyncSession, *, cache: TariffCache | None = None, tenant_id: UUID | None = None
    ) -> None:
        self._s = session
        self._cache = cache if tenant_id is not None else None
        self._tenant_id = tenant_id

    async def default_version_at(self, lot_id: UUID, at: datetime) -> VersionRecord | None:
        """Версия тарифа по умолчанию, действующая в `at`, вместе с праздниками — один SQL, на горячем пути через кеш."""
        key = (self._tenant_id, lot_id) if self._tenant_id is not None else None
        if self._cache is not None and key is not None:
            cached = self._cache.get(key)
            if cached is not None and cached.covers(at):
                return cached
        row = (await self._s.execute(_VERSION_AT, {"lot": lot_id, "at": at})).first()
        if row is None:
            return None
        rules = row.rules if isinstance(row.rules, dict) else json.loads(row.rules)
        record = VersionRecord(
            id=row.id,
            plan_id=row.plan_id,
            lot_id=row.lot_id,
            version=row.version,
            rules=rules,
            holiday_calendar_id=row.holiday_calendar_id,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            holidays=frozenset(row.holidays or ()),
        )
        if self._cache is not None and key is not None:
            self._cache.set(key, record)
        return record

    def _invalidate(self, tenant_id: UUID) -> None:
        """Любая запись тарифа сбрасывает кеш tenant'а в этом процессе; другие поды — не позже TTL."""
        if self._cache is not None:
            self._cache.invalidate_where(lambda k: k[0] == tenant_id)

    async def list_plans(self, lot_id: UUID) -> list[TariffPlanView]:
        rows = await self._s.execute(
            text(
                "SELECT id, lot_id, code, name, is_default, active FROM tariff_plan WHERE lot_id = :lot ORDER BY code"
            ),
            {"lot": lot_id},
        )
        return [_plan(r) for r in rows]

    async def get_plan(self, plan_id: UUID) -> TariffPlanView | None:
        row = (
            await self._s.execute(
                text("SELECT id, lot_id, code, name, is_default, active FROM tariff_plan WHERE id = :id"),
                {"id": plan_id},
            )
        ).first()
        return None if row is None else _plan(row)

    async def create_plan(
        self, *, plan_id: UUID, tenant_id: UUID, lot_id: UUID, code: str, name: str, is_default: bool
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO tariff_plan (id, tenant_id, lot_id, code, name, is_default) VALUES (:id, :tenant, :lot, :code, :name, :is_default)"
            ),
            {"id": plan_id, "tenant": tenant_id, "lot": lot_id, "code": code, "name": name, "is_default": is_default},
        )
        self._invalidate(tenant_id)

    async def list_versions(self, plan_id: UUID) -> list[TariffVersionView]:
        rows = await self._s.execute(
            text(
                """
                SELECT id, plan_id, version, rules, lower(validity) AS valid_from, upper(validity) AS valid_to, holiday_calendar_id, created_at
                FROM tariff_plan_version WHERE plan_id = :plan ORDER BY version
                """
            ),
            {"plan": plan_id},
        )
        return [
            TariffVersionView(
                id=r.id,
                plan_id=r.plan_id,
                version=r.version,
                rules=r.rules if isinstance(r.rules, dict) else json.loads(r.rules),
                valid_from=r.valid_from,
                valid_to=r.valid_to,
                holiday_calendar_id=r.holiday_calendar_id,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def close_open_version(self, plan_id: UUID, at: datetime) -> None:
        await self._s.execute(
            text(
                """
                UPDATE tariff_plan_version SET validity = tstzrange(lower(validity), CAST(:at AS timestamptz), '[)')
                WHERE plan_id = :plan AND upper_inf(validity) AND lower(validity) < CAST(:at AS timestamptz)
                """
            ),
            {"plan": plan_id, "at": at},
        )

    async def add_version(
        self,
        *,
        version_id: UUID,
        tenant_id: UUID,
        plan_id: UUID,
        rules: dict[str, Any],
        valid_from: datetime,
        valid_to: datetime | None,
        holiday_calendar_id: UUID | None,
        created_by: UUID,
    ) -> int:
        try:
            row = await self._insert_version(
                version_id=version_id,
                tenant_id=tenant_id,
                plan_id=plan_id,
                rules=rules,
                valid_from=valid_from,
                valid_to=valid_to,
                holiday_calendar_id=holiday_calendar_id,
                created_by=created_by,
            )
        except IntegrityError as exc:
            if "ex_tpv_no_overlap" in str(exc.orig):
                raise ConflictError("tariff version overlaps an existing one (I-12)") from None
            raise
        self._invalidate(tenant_id)
        return row

    async def _insert_version(
        self,
        *,
        version_id: UUID,
        tenant_id: UUID,
        plan_id: UUID,
        rules: dict[str, Any],
        valid_from: datetime,
        valid_to: datetime | None,
        holiday_calendar_id: UUID | None,
        created_by: UUID,
    ) -> int:
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO tariff_plan_version (id, tenant_id, plan_id, version, rules, validity, holiday_calendar_id, created_by)
                    VALUES (:id, :tenant, :plan, (SELECT coalesce(max(version), 0) + 1 FROM tariff_plan_version WHERE plan_id = :plan),
                            CAST(:rules AS jsonb), tstzrange(CAST(:valid_from AS timestamptz), CAST(:valid_to AS timestamptz), '[)'),
                            :calendar, :created_by)
                    RETURNING version
                    """
                ),
                {
                    "id": version_id,
                    "tenant": tenant_id,
                    "plan": plan_id,
                    "rules": json.dumps(rules),
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "calendar": holiday_calendar_id,
                    "created_by": created_by,
                },
            )
        ).one()
        return int(row.version)


def _plan(r: Any) -> TariffPlanView:
    return TariffPlanView(id=r.id, lot_id=r.lot_id, code=r.code, name=r.name, is_default=r.is_default, active=r.active)
