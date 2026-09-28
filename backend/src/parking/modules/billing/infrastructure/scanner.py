"""Кросс-tenant списки для джобов платежей (роль app_maintenance, собственные короткие транзакции)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.billing.domain.payment import Provider


class SqlBillingScanner:
    def __init__(self, maintenance_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = maintenance_factory

    async def tenants_with_providers(self) -> list[tuple[UUID, Provider]]:
        async with self._factory() as session, session.begin():
            rows = await session.execute(
                text(
                    "SELECT tenant_id, provider::text AS provider FROM provider_account WHERE active ORDER BY tenant_id"
                )
            )
            return [(r.tenant_id, Provider(r.provider)) for r in rows]

    async def active_tenants(self) -> list[UUID]:
        async with self._factory() as session, session.begin():
            rows = await session.execute(text("SELECT id FROM tenant WHERE active ORDER BY id"))
            return [r.id for r in rows]
