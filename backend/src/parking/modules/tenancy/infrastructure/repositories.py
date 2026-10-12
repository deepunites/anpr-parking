from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.tenancy.application.contracts import LotInfo
from parking.modules.tenancy.domain.lot import LotPolicies, ParkingLot, UnknownExitPolicy
from parking.shared.ttl_cache import TtlCache

LotCache = TtlCache[tuple[UUID, UUID], LotInfo]
"""Кеш процесса (tenant_id, lot_id) → LotInfo: ключ с tenant_id, чтобы кеш не обходил RLS."""

_COLUMNS = """
    id, tenant_id, code, name, address, capacity, timezone, active, max_session_hours, reentry_window_minutes,
    unknown_exit_policy::text AS unknown_exit_policy, deny_entry_when_full, exit_grace_minutes, exit_inferred_after_minutes,
    late_event_window_hours, barrier_cidr_allowlist::text[] AS barrier_cidr_allowlist, show_full_plates
"""


def _policies(row: Any) -> LotPolicies:
    return LotPolicies(
        max_session_hours=row.max_session_hours,
        reentry_window_minutes=row.reentry_window_minutes,
        unknown_exit_policy=UnknownExitPolicy(row.unknown_exit_policy),
        deny_entry_when_full=row.deny_entry_when_full,
        exit_grace_minutes=row.exit_grace_minutes,
        exit_inferred_after_minutes=row.exit_inferred_after_minutes,
        late_event_window_hours=row.late_event_window_hours,
        barrier_cidr_allowlist=tuple(row.barrier_cidr_allowlist or ()),
        show_full_plates=row.show_full_plates,
    )


def _to_lot(row: Any) -> ParkingLot:
    return ParkingLot(
        id=row.id,
        tenant_id=row.tenant_id,
        code=row.code,
        name=row.name,
        capacity=row.capacity,
        timezone=row.timezone,
        address=row.address,
        active=row.active,
        policies=_policies(row),
    )


class SqlLotRepository:
    def __init__(self, session: AsyncSession, *, cache: LotCache | None = None) -> None:
        self._s = session
        self._cache = cache

    async def get(self, lot_id: UUID) -> ParkingLot | None:
        row = (
            await self._s.execute(text(f"SELECT {_COLUMNS} FROM parking_lot WHERE id = :id"), {"id": lot_id})
        ).first()
        return None if row is None else _to_lot(row)

    async def get_for_update(self, lot_id: UUID) -> ParkingLot | None:
        row = (
            await self._s.execute(text(f"SELECT {_COLUMNS} FROM parking_lot WHERE id = :id FOR UPDATE"), {"id": lot_id})
        ).first()
        return None if row is None else _to_lot(row)

    async def list(self, *, only: frozenset[UUID] | None) -> list[ParkingLot]:
        if only is not None:
            rows = await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM parking_lot WHERE id = ANY(:ids) ORDER BY code"), {"ids": list(only)}
            )
        else:
            rows = await self._s.execute(text(f"SELECT {_COLUMNS} FROM parking_lot ORDER BY code"))
        return [_to_lot(r) for r in rows]

    async def add(self, lot: ParkingLot) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO parking_lot (id, tenant_id, code, name, address, capacity, timezone, active, max_session_hours,
                    reentry_window_minutes, unknown_exit_policy, deny_entry_when_full, exit_grace_minutes,
                    exit_inferred_after_minutes, late_event_window_hours, barrier_cidr_allowlist, show_full_plates)
                VALUES (:id, :tenant_id, :code, :name, :address, :capacity, :timezone, :active, :max_session_hours,
                    :reentry_window_minutes, CAST(:unknown_exit_policy AS unknown_exit_policy), :deny_entry_when_full,
                    :exit_grace_minutes, :exit_inferred_after_minutes, :late_event_window_hours, CAST(:allowlist AS cidr[]),
                    :show_full_plates)
                """
            ),
            _params(lot),
        )
        self._invalidate(lot)

    async def save(self, lot: ParkingLot) -> None:
        await self._s.execute(
            text(
                """
                UPDATE parking_lot SET name = :name, address = :address, capacity = :capacity, active = :active,
                    max_session_hours = :max_session_hours, reentry_window_minutes = :reentry_window_minutes,
                    unknown_exit_policy = CAST(:unknown_exit_policy AS unknown_exit_policy), deny_entry_when_full = :deny_entry_when_full,
                    exit_grace_minutes = :exit_grace_minutes, exit_inferred_after_minutes = :exit_inferred_after_minutes,
                    late_event_window_hours = :late_event_window_hours, barrier_cidr_allowlist = CAST(:allowlist AS cidr[]),
                    show_full_plates = :show_full_plates
                WHERE id = :id
                """
            ),
            _params(lot),
        )
        self._invalidate(lot)

    def _invalidate(self, lot: ParkingLot) -> None:
        """Сброс записи этого процесса до коммита: другие поды увидят изменение не позже TTL (см. shared.ttl_cache)."""
        if self._cache is not None:
            self._cache.invalidate((lot.tenant_id, lot.id))


def _to_info(row: Any) -> LotInfo:
    return LotInfo(
        id=row.id,
        tenant_id=row.tenant_id,
        code=row.code,
        timezone=row.timezone,
        capacity=row.capacity,
        active=row.active,
        policies=_policies(row),
    )


class SqlLotDirectory:
    """Контракт для других модулей: только чтение.

    С `cache` и `tenant_id` (tenant текущей UoW) чтение идёт через кеш процесса — горячий путь camera-события
    (ENTRY/EXIT/тариф) не ходит в `parking_lot` на каждое событие. Без tenant_id (системный контекст) кеш не используется.
    """

    def __init__(self, session: AsyncSession, *, cache: LotCache | None = None, tenant_id: UUID | None = None) -> None:
        self._s = session
        self._cache = cache if tenant_id is not None else None
        self._tenant_id = tenant_id

    async def get(self, lot_id: UUID) -> LotInfo | None:
        key = (self._tenant_id, lot_id) if self._tenant_id is not None else None
        if self._cache is not None and key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        row = (
            await self._s.execute(text(f"SELECT {_COLUMNS} FROM parking_lot WHERE id = :id"), {"id": lot_id})
        ).first()
        if row is None:
            return None  # «нет площадки» не кешируем: после создания она видна сразу
        info = _to_info(row)
        if self._cache is not None and key is not None and info.tenant_id == self._tenant_id:
            self._cache.set(key, info)
        return info

    async def shows_full_plates(self) -> bool:
        return bool(
            (
                await self._s.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM parking_lot WHERE active AND show_full_plates)"
                    )  # RLS: свой tenant
                )
            ).scalar()
        )


def _params(lot: ParkingLot) -> dict[str, Any]:
    p = lot.policies
    return {
        "id": lot.id,
        "tenant_id": lot.tenant_id,
        "code": lot.code,
        "name": lot.name,
        "address": lot.address,
        "capacity": lot.capacity,
        "timezone": lot.timezone,
        "active": lot.active,
        "max_session_hours": p.max_session_hours,
        "reentry_window_minutes": p.reentry_window_minutes,
        "unknown_exit_policy": p.unknown_exit_policy.value,
        "deny_entry_when_full": p.deny_entry_when_full,
        "exit_grace_minutes": p.exit_grace_minutes,
        "exit_inferred_after_minutes": p.exit_inferred_after_minutes,
        "late_event_window_hours": p.late_event_window_hours,
        "allowlist": list(p.barrier_cidr_allowlist),
        "show_full_plates": p.show_full_plates,
    }
