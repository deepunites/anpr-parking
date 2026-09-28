"""Чтение сессий с обязательным фильтром tenant_id — первый рубеж изоляции (RLS — второй)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    vehicle_id: UUID
    entry_time: datetime
    status: str


class SessionReader:
    """Запросы к parking_session. Каждый запрос фильтрует tenant_id явно, вне зависимости от RLS."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def find_active(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes) -> SessionRow | None:
        """Горячий путь EXIT (§3.1): активная сессия по (tenant, lot, plate_hash) через vehicle."""
        row = await self._conn.fetchrow(
            """
            SELECT s.id, s.tenant_id, s.lot_id, s.vehicle_id, s.entry_time, s.status::text AS status
            FROM vehicle v
            JOIN parking_session s ON s.vehicle_id = v.id AND s.lot_id = $2 AND s.status = 'ACTIVE'
            WHERE v.tenant_id = $1 AND v.plate_hash = $3 AND s.tenant_id = $1
            """,
            tenant_id,
            lot_id,
            plate_hash,
        )
        return None if row is None else _to_row(row)

    async def list_by_lot(self, tenant_id: UUID, lot_id: UUID, limit: int = 50) -> list[SessionRow]:
        """Лента площадки (§3.8, первая страница)."""
        rows = await self._conn.fetch(
            """
            SELECT id, tenant_id, lot_id, vehicle_id, entry_time, status::text AS status
            FROM parking_session
            WHERE tenant_id = $1 AND lot_id = $2
            ORDER BY entry_time DESC, id DESC
            LIMIT $3
            """,
            tenant_id,
            lot_id,
            limit,
        )
        return [_to_row(r) for r in rows]


def _to_row(r: asyncpg.Record) -> SessionRow:
    return SessionRow(
        id=r["id"],
        tenant_id=r["tenant_id"],
        lot_id=r["lot_id"],
        vehicle_id=r["vehicle_id"],
        entry_time=r["entry_time"],
        status=r["status"],
    )
