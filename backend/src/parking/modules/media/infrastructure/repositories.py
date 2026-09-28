from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SqlMediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_expected(self, *, key: str, tenant_id: UUID, lot_id: UUID, camera_id: UUID, at: datetime) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO media_object (object_key, created_at, tenant_id, lot_id, camera_id, status)
                VALUES (:key, :at, :tenant, :lot, :camera, 'EXPECTED')
                """
            ),
            {"key": key, "at": at, "tenant": tenant_id, "lot": lot_id, "camera": camera_id},
        )

    async def mark_delivered(self, key: str, *, size_bytes: int, at: datetime) -> None:
        await self._s.execute(
            text(
                "UPDATE media_object SET status = 'DELIVERED', delivered_at = :at, size_bytes = :size WHERE object_key = :key"
            ),
            {"key": key, "at": at, "size": size_bytes},
        )
