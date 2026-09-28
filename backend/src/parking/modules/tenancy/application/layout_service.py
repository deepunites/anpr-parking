"""v2 этап 5: схема площадки — чтение, сохранение новой версии (оптимистичная блокировка по base_version),
история версий. Устройства на схеме проверяются по списку камер/стрел площадки, который передаёт вызывающий
(модуль devices — не зависимость tenancy)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.tenancy.application.ports import LayoutRecord, LayoutRepository, LotRepository
from parking.modules.tenancy.domain.layout import EMPTY_LAYOUT, LotLayout
from parking.platform.db.uow import UnitOfWork
from parking.shared.errors import ConflictError, NotFoundError, ValidationError

HISTORY_LIMIT = 50


@dataclass(frozen=True, slots=True)
class LayoutRepositories:
    lots: LotRepository
    layouts: LayoutRepository
    audit: AuditWriter


@dataclass(frozen=True, slots=True)
class LayoutView:
    lot_id: UUID
    version: int  # 0 — схемы ещё нет
    document: dict[str, Any]
    spots: int
    capacity: int  # вместимость площадки из настроек — редактор сравнивает с числом мест на схеме
    updated_at: datetime | None
    updated_by: UUID | None

    def parsed(self) -> LotLayout:
        return LotLayout.parse(self.document)


class LayoutService:
    def __init__(
        self, *, uow_factory: Callable[[UUID | None], UnitOfWork], repos: Callable[[UnitOfWork], LayoutRepositories]
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos

    @staticmethod
    def _scope(actor: CurrentUser, lot_id: UUID) -> None:
        allowed = actor.allowed_lots()
        if allowed is not None and lot_id not in allowed:
            raise NotFoundError("lot not found")

    async def get(self, actor: CurrentUser, lot_id: UUID) -> LayoutView:
        self._scope(actor, lot_id)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get(lot_id)
            if lot is None:
                raise NotFoundError("lot not found")
            record = await repos.layouts.current(lot_id)
        return _view(lot_id, record, lot.capacity)

    async def get_version(self, actor: CurrentUser, lot_id: UUID, version: int) -> LayoutView:
        self._scope(actor, lot_id)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get(lot_id)
            record = await repos.layouts.version(lot_id, version) if lot is not None else None
        if lot is None or record is None:
            raise NotFoundError("layout version not found")
        return _view(lot_id, record, lot.capacity)

    async def history(self, actor: CurrentUser, lot_id: UUID) -> list[LayoutRecord]:
        self._scope(actor, lot_id)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")
            return await repos.layouts.history(lot_id, limit=HISTORY_LIMIT)

    async def save(
        self,
        actor: CurrentUser,
        lot_id: UUID,
        *,
        base_version: int,
        document: dict[str, Any],
        known_devices: set[tuple[str, UUID]],
        reason: str | None,
    ) -> LayoutView:
        """Новая версия схемы. base_version — версия, которую редактор открыл: если с тех пор схему сохранил
        кто-то другой, 409 LAYOUT_CONFLICT (без молчаливой перезаписи чужой правки)."""
        self._scope(actor, lot_id)
        layout = LotLayout.parse(document)
        unknown = layout.device_refs() - known_devices
        if unknown:
            kind, ref = sorted(unknown, key=str)[0]
            raise ValidationError(f"unknown {kind} on layout: {ref}", code="UNKNOWN_DEVICE")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get_for_update(lot_id)  # сериализует сохранения одной площадки
            if lot is None:
                raise NotFoundError("lot not found")
            current = await repos.layouts.current(lot_id)
            current_version = current.version if current is not None else 0
            if base_version != current_version:
                raise ConflictError(
                    "layout was changed by someone else",
                    code="LAYOUT_CONFLICT",
                    details={"current_version": current_version},
                )
            clean = layout.to_dict()
            version = current_version + 1
            await repos.layouts.add(
                tenant_id=actor.tenant_id,
                lot_id=lot_id,
                version=version,
                document=clean,
                spots=len(layout.spots),
                created_by=actor.user_id,
                reason=reason,
            )
            await repos.layouts.sync_spots(actor.tenant_id, lot_id, layout.spots, {z.id: z.name for z in layout.zones})
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="LAYOUT_UPDATED",
                    entity_type="parking_lot",
                    entity_id=lot_id,
                    lot_id=lot_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason or "правка схемы площадки",
                    payload={"version": version, "zones": len(layout.zones), "spots": len(layout.spots)},
                )
            )
            record = await repos.layouts.current(lot_id)
        return _view(lot_id, record, lot.capacity)


def _view(lot_id: UUID, record: LayoutRecord | None, capacity: int) -> LayoutView:
    if record is None:
        return LayoutView(lot_id, 0, dict(EMPTY_LAYOUT), 0, capacity, None, None)
    return LayoutView(
        lot_id, record.version, record.document, record.spots, capacity, record.created_at, record.created_by
    )
