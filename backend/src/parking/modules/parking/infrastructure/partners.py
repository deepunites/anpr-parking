"""Партнёры площадки (клуб): партнёры, их сотрудники-бармены и отметки «выезд за счёт партнёра» (RLS по tenant)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.parking.application.ports import Partner, PartnerMember, ValidationRecord, ValidationRow

_PARTNER = "SELECT id, tenant_id, lot_id, name, price_per_car, monthly_fee, active FROM partner"


def _partner(r: Any) -> Partner:
    return Partner(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        name=r.name,
        price_per_car=int(r.price_per_car),
        active=bool(r.active),
        monthly_fee=int(r.monthly_fee),
    )


class SqlPartnerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def all(self) -> list[Partner]:
        rows = await self._s.execute(text(f"{_PARTNER} ORDER BY active DESC, name"))
        return [_partner(r) for r in rows]

    async def get(self, partner_id: UUID, *, for_update: bool = False) -> Partner | None:
        query = f"{_PARTNER} WHERE id = :id FOR UPDATE" if for_update else f"{_PARTNER} WHERE id = :id"
        row = (await self._s.execute(text(query), {"id": partner_id})).first()
        return None if row is None else _partner(row)

    async def add(self, partner: Partner) -> None:
        await self._s.execute(
            text(
                "INSERT INTO partner (id, tenant_id, lot_id, name, price_per_car, monthly_fee, active) "
                "VALUES (:id, :tenant, :lot, :name, :price, :fee, :active)"
            ),
            {
                "id": partner.id,
                "tenant": partner.tenant_id,
                "lot": partner.lot_id,
                "name": partner.name,
                "price": partner.price_per_car,
                "fee": partner.monthly_fee,
                "active": partner.active,
            },
        )

    async def save(self, partner: Partner) -> None:
        await self._s.execute(
            text(
                "UPDATE partner SET name = :name, price_per_car = :price, monthly_fee = :fee, active = :active "
                "WHERE id = :id"
            ),
            {
                "id": partner.id,
                "name": partner.name,
                "price": partner.price_per_car,
                "fee": partner.monthly_fee,
                "active": partner.active,
            },
        )

    async def of_user(self, user_id: UUID) -> Partner | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT p.id, p.tenant_id, p.lot_id, p.name, p.price_per_car, p.monthly_fee, p.active "
                    "FROM partner_member m JOIN partner p ON p.id = m.partner_id WHERE m.user_id = :u"
                ),
                {"u": user_id},
            )
        ).first()
        return None if row is None else _partner(row)

    async def add_member(self, tenant_id: UUID, partner_id: UUID, user_id: UUID) -> None:
        await self._s.execute(
            text(
                "INSERT INTO partner_member (tenant_id, partner_id, user_id) VALUES (:t, :p, :u) "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE SET partner_id = EXCLUDED.partner_id"
            ),
            {"t": tenant_id, "p": partner_id, "u": user_id},
        )

    async def members(self, partner_id: UUID) -> list[PartnerMember]:
        rows = await self._s.execute(
            text(
                "SELECT u.id, u.display_name, coalesce(u.email, u.phone) AS login, u.active "
                "FROM partner_member m JOIN app_user u ON u.id = m.user_id WHERE m.partner_id = :p "
                "ORDER BY u.display_name"
            ),
            {"p": partner_id},
        )
        return [PartnerMember(user_id=r.id, display_name=r.display_name, login=r.login, active=r.active) for r in rows]

    async def add_validation(
        self, *, validation_id: UUID, partner: Partner, session_id: UUID, created_by: UUID
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO partner_validation (id, tenant_id, lot_id, partner_id, session_id, price_per_car, created_by) "
                "VALUES (:id, :tenant, :lot, :partner, :session, :price, :by)"
            ),
            {
                "id": validation_id,
                "tenant": partner.tenant_id,
                "lot": partner.lot_id,
                "partner": partner.id,
                "session": session_id,
                "price": partner.price_per_car,
                "by": created_by,
            },
        )

    async def get_validation(self, validation_id: UUID, *, for_update: bool = False) -> ValidationRecord | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT id, partner_id, session_id, cancelled_at FROM partner_validation WHERE id = :id{lock}"),
                {"id": validation_id},
            )
        ).first()
        if row is None:
            return None
        return ValidationRecord(
            id=row.id, partner_id=row.partner_id, session_id=row.session_id, cancelled_at=row.cancelled_at
        )

    async def cancel_validation(self, validation_id: UUID, *, by: UUID, reason: str, at: datetime) -> None:
        await self._s.execute(
            text(
                "UPDATE partner_validation SET cancelled_at = :at, cancelled_by = :by, cancel_reason = :reason "
                "WHERE id = :id AND cancelled_at IS NULL"
            ),
            {"id": validation_id, "at": at, "by": by, "reason": reason.strip()},
        )

    async def validations(self, partner_id: UUID, *, since: datetime, until: datetime) -> list[ValidationRow]:
        rows = await self._s.execute(
            text(
                """
                SELECT pv.id, pv.session_id, v.plate_encrypted, v.enc_key_version, s.entry_time, s.exit_time,
                       s.status::text AS session_status, pv.price_per_car, pv.created_at, u.display_name AS created_by,
                       pv.cancelled_at, pv.cancel_reason
                FROM partner_validation pv
                JOIN parking_session s ON s.id = pv.session_id
                JOIN vehicle v ON v.id = s.vehicle_id
                LEFT JOIN app_user u ON u.id = pv.created_by
                WHERE pv.partner_id = :p AND pv.created_at >= :since AND pv.created_at < :until
                ORDER BY pv.created_at DESC
                """
            ),
            {"p": partner_id, "since": since, "until": until},
        )
        return [
            ValidationRow(
                id=r.id,
                session_id=r.session_id,
                plate_encrypted=bytes(r.plate_encrypted),
                enc_key_version=int(r.enc_key_version),
                entry_time=r.entry_time,
                exit_time=r.exit_time,
                session_status=r.session_status,
                price_per_car=int(r.price_per_car),
                created_at=r.created_at,
                created_by=r.created_by,
                cancelled_at=r.cancelled_at,
                cancel_reason=r.cancel_reason,
            )
            for r in rows
        ]
