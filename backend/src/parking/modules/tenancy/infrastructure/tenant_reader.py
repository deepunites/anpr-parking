from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class TenantInfo:
    id: UUID
    code: str
    name: str
    currency: str
    timezone: str
    active: bool


class SqlTenantReader:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def current(self, tenant_id: UUID) -> TenantInfo | None:
        row = (
            await self._s.execute(
                text("SELECT id, code, name, currency, timezone, active FROM tenant WHERE id = :id"), {"id": tenant_id}
            )
        ).first()
        if row is None:
            return None
        return TenantInfo(
            id=row.id,
            code=row.code,
            name=row.name,
            currency=row.currency.strip(),
            timezone=row.timezone,
            active=row.active,
        )
