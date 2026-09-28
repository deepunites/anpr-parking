"""v2 этап 5: SQL-адаптер схемы площадки — версии lot_layout и синхронизация мест parking_spot."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.tenancy.application.ports import LayoutRecord
from parking.modules.tenancy.domain.layout import Spot


def _record(row: Any) -> LayoutRecord:
    doc = row.document if isinstance(row.document, dict) else json.loads(row.document)
    return LayoutRecord(
        lot_id=row.lot_id,
        version=row.version,
        document=doc,
        spots=row.spots,
        created_by=row.created_by,
        reason=row.reason,
        created_at=row.created_at,
    )


_COLUMNS = "lot_id, version, document, spots, created_by, reason, created_at"


class SqlLayoutRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def current(self, lot_id: UUID) -> LayoutRecord | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM lot_layout WHERE lot_id = :lot ORDER BY version DESC LIMIT 1"),
                {"lot": lot_id},
            )
        ).first()
        return None if row is None else _record(row)

    async def version(self, lot_id: UUID, version: int) -> LayoutRecord | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM lot_layout WHERE lot_id = :lot AND version = :v"),
                {"lot": lot_id, "v": version},
            )
        ).first()
        return None if row is None else _record(row)

    async def history(self, lot_id: UUID, *, limit: int) -> list[LayoutRecord]:
        rows = await self._s.execute(
            text(f"SELECT {_COLUMNS} FROM lot_layout WHERE lot_id = :lot ORDER BY version DESC LIMIT :limit"),
            {"lot": lot_id, "limit": limit},
        )
        return [_record(r) for r in rows]

    async def add(
        self,
        *,
        tenant_id: UUID,
        lot_id: UUID,
        version: int,
        document: dict[str, Any],
        spots: int,
        created_by: UUID,
        reason: str | None,
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO lot_layout (tenant_id, lot_id, version, document, spots, created_by, reason) "
                "VALUES (:t, :lot, :v, CAST(:doc AS jsonb), :spots, :by, :reason)"
            ),
            {
                "t": tenant_id,
                "lot": lot_id,
                "v": version,
                "doc": json.dumps(document, ensure_ascii=False),
                "spots": spots,
                "by": created_by,
                "reason": reason,
            },
        )

    async def sync_spots(
        self, tenant_id: UUID, lot_id: UUID, spots: tuple[Spot, ...], zone_names: dict[str, str]
    ) -> None:
        """Места схемы → parking_spot: upsert по (lot_id, code); исчезнувшие со схемы — active = false (история цела)."""
        if spots:  # пакетом (executemany): схема на тысячи мест — один проход, не тысячи запросов
            await self._s.execute(
                text(
                    "INSERT INTO parking_spot (tenant_id, lot_id, code, zone, x, y, active) "
                    "VALUES (:t, :lot, :code, :zone, :x, :y, true) "
                    "ON CONFLICT (lot_id, code) DO UPDATE SET zone = EXCLUDED.zone, x = EXCLUDED.x, y = EXCLUDED.y, "
                    "active = true"
                ),
                [
                    {
                        "t": tenant_id,
                        "lot": lot_id,
                        "code": spot.code,
                        "zone": zone_names.get(spot.zone_id or ""),
                        "x": spot.x,
                        "y": spot.y,
                    }
                    for spot in spots
                ],
            )
        await self._s.execute(
            text("UPDATE parking_spot SET active = false WHERE lot_id = :lot AND active AND NOT (code = ANY(:codes))"),
            {"lot": lot_id, "codes": [s.code for s in spots]},
        )
