from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.tenancy.application.contracts import LotView, SubscriptionGate
from parking.modules.tenancy.application.ports import LotRepository
from parking.modules.tenancy.domain.lot import LotPolicies, ParkingLot
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.shared.errors import ConflictError, NotFoundError


@dataclass(frozen=True, slots=True)
class TenancyRepositories:
    lots: LotRepository
    audit: AuditWriter


@dataclass(frozen=True, slots=True)
class CreateLotCommand:
    code: str
    name: str
    capacity: int
    timezone: str
    address: str | None = None
    policies: LotPolicies = field(default_factory=LotPolicies)


@dataclass(frozen=True, slots=True)
class UpdateLotCommand:
    name: str | None = None
    capacity: int | None = None
    address: str | None = None
    policies: LotPolicies | None = None


class LotService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], TenancyRepositories],
        gate: SubscriptionGate | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._gate = gate  # v2: лимиты тарифа платформы (площадки, места)

    async def list(self, actor: CurrentUser) -> list[LotView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return [LotView.of(lot) for lot in await self._repos(uow).lots.list(only=actor.allowed_lots())]

    async def get(self, actor: CurrentUser, lot_id: UUID) -> LotView:
        """Чужой tenant или площадка вне скоупа → 404, не 403: существование не подтверждается."""
        allowed = actor.allowed_lots()
        if allowed is not None and lot_id not in allowed:
            raise NotFoundError("lot not found")
        async with self._uow_factory(actor.tenant_id) as uow:
            lot = await self._repos(uow).lots.get(lot_id)
        if lot is None:
            raise NotFoundError("lot not found")
        return LotView.of(lot)

    async def create(self, actor: CurrentUser, cmd: CreateLotCommand, *, reason: str) -> LotView:
        lot = ParkingLot(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            code=cmd.code,
            name=cmd.name,
            capacity=cmd.capacity,
            timezone=cmd.timezone,
            address=cmd.address,
            policies=cmd.policies,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            existing_lots = await repos.lots.list(only=None)
            if any(existing.code == lot.code for existing in existing_lots):
                raise ConflictError("lot code already exists")
            if self._gate is not None:
                active = [x for x in existing_lots if x.active]
                await self._gate.ensure(actor.tenant_id, "lots", len(active) + 1)
                await self._gate.ensure(actor.tenant_id, "places", sum(x.capacity for x in active) + lot.capacity)
            await repos.lots.add(lot)
            await self._audit(repos, actor, "LOT_CREATED", lot, reason)
            uow.publish(self._event(lot))
        return LotView.of(lot)

    async def update(self, actor: CurrentUser, lot_id: UUID, cmd: UpdateLotCommand, *, reason: str) -> LotView:
        allowed = actor.allowed_lots()
        if allowed is not None and lot_id not in allowed:
            raise NotFoundError("lot not found")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get_for_update(lot_id)
            if lot is None:
                raise NotFoundError("lot not found")
            if self._gate is not None and cmd.capacity is not None and cmd.capacity > lot.capacity:
                others = [x for x in await repos.lots.list(only=None) if x.active and x.id != lot.id]
                await self._gate.ensure(actor.tenant_id, "places", sum(x.capacity for x in others) + cmd.capacity)
            lot.update(name=cmd.name, capacity=cmd.capacity, address=cmd.address, policies=cmd.policies)
            await repos.lots.save(lot)
            await self._audit(repos, actor, "LOT_UPDATED", lot, reason)
            uow.publish(self._event(lot))
        return LotView.of(lot)

    @staticmethod
    def _event(lot: ParkingLot) -> IntegrationEvent:
        return IntegrationEvent(
            type="tenancy.lot.updated.v1",
            tenant_id=lot.tenant_id,
            aggregate_type="parking_lot",
            aggregate_id=lot.id,
            payload={
                "lot_id": str(lot.id),
                "code": lot.code,
                "capacity": lot.capacity,
                "active": lot.active,
                "timezone": lot.timezone,
            },
        )

    @staticmethod
    async def _audit(repos: TenancyRepositories, actor: CurrentUser, action: str, lot: ParkingLot, reason: str) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="parking_lot",
                entity_id=lot.id,
                lot_id=lot.id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload={"code": lot.code, "capacity": lot.capacity},
            )
        )
