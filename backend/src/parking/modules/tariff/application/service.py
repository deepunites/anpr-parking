from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.tariff.application.contracts import TariffCalculation, TariffPlanView, TariffVersionView
from parking.modules.tariff.application.ports import TariffRepository
from parking.modules.tariff.domain.engine import AccessTerms, HolidayCalendar, calculate
from parking.modules.tariff.domain.rules import TariffRules
from parking.modules.tenancy.application.contracts import LotDirectory
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.tracing import span
from parking.shared.errors import ConflictError, NotFoundError, ValidationError


@dataclass(frozen=True, slots=True)
class TariffRepositories:
    tariffs: TariffRepository
    lots: LotDirectory
    audit: AuditWriter


class SqlTariffCalculator:
    """Реализация контракта TariffCalculator поверх открытой UoW (вызывается из саги в её транзакции)."""

    def __init__(self, repos: TariffRepositories) -> None:
        self._repos = repos

    async def calculate(self, lot_id: UUID, entry: datetime, exit: datetime, access: AccessTerms) -> TariffCalculation:
        with span("tariff.calculate", lot_id=lot_id, access_kind=access.kind.value):
            return await self._calculate_traced(lot_id, entry, exit, access)

    async def _calculate_traced(
        self, lot_id: UUID, entry: datetime, exit: datetime, access: AccessTerms
    ) -> TariffCalculation:
        lot = await self._repos.lots.get(lot_id)
        if lot is None:
            raise NotFoundError("lot not found")
        version = await self._repos.tariffs.default_version_at(lot_id, exit)
        if version is None:
            raise NotFoundError("no tariff version valid at exit time")
        rules = TariffRules.from_json(version.rules)
        calc = calculate(entry, exit, rules, access, HolidayCalendar(version.holidays), ZoneInfo(lot.timezone))
        return replace(calc, tariff_plan_version_id=version.id)


@dataclass(frozen=True, slots=True)
class PublishVersionCommand:
    plan_id: UUID
    rules: dict[str, Any]
    valid_from: datetime
    valid_to: datetime | None = None
    holiday_calendar_id: UUID | None = None


class TariffAdminService:
    def __init__(
        self, *, uow_factory: Callable[[UUID | None], UnitOfWork], repos: Callable[[UnitOfWork], TariffRepositories]
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos

    async def quote(
        self, actor: CurrentUser, lot_id: UUID, entry: datetime, exit: datetime, access: AccessTerms
    ) -> TariffCalculation:
        if actor.allowed_lots() is not None and lot_id not in (actor.allowed_lots() or ()):
            raise NotFoundError("lot not found")
        async with self._uow_factory(actor.tenant_id) as uow:
            return await SqlTariffCalculator(self._repos(uow)).calculate(lot_id, entry, exit, access)

    async def preview(
        self, actor: CurrentUser, lot_id: UUID, rules: dict[str, Any], *, start: datetime, durations: Sequence[int]
    ) -> list[TariffCalculation]:
        """Черновик тарифа до публикации («сколько заплатят за 30 мин, 1 ч, сутки»): тот же разбор правил и движок,
        что у оплаты, в часовом поясе площадки; ничего не записывается. Праздничный календарь не учитывается."""
        parsed = TariffRules.from_json(rules)
        if start.tzinfo is None:  # иначе ensure_aware движка — ValueError и 500
            raise ValidationError("start must include a timezone offset")
        if actor.allowed_lots() is not None and lot_id not in (actor.allowed_lots() or ()):
            raise NotFoundError("lot not found")
        async with self._uow_factory(actor.tenant_id) as uow:
            lot = await self._repos(uow).lots.get(lot_id)
        if lot is None:
            raise NotFoundError("lot not found")
        tz = ZoneInfo(lot.timezone)
        try:
            return [
                calculate(start, start + timedelta(minutes=m), parsed, AccessTerms(), HolidayCalendar(), tz)
                for m in durations
            ]
        except OverflowError as exc:  # start около 9999 года
            raise ValidationError("start is out of range") from exc

    async def list_plans(self, actor: CurrentUser, lot_id: UUID) -> list[TariffPlanView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")
            return await repos.tariffs.list_plans(lot_id)

    async def create_plan(
        self, actor: CurrentUser, lot_id: UUID, *, code: str, name: str, is_default: bool, reason: str
    ) -> TariffPlanView:
        plan_id = uuid4()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")
            if any(p.code == code for p in await repos.tariffs.list_plans(lot_id)):
                raise ConflictError("plan code already exists for this lot")
            if is_default and any(p.is_default for p in await repos.tariffs.list_plans(lot_id)):
                raise ConflictError("lot already has a default plan")
            await repos.tariffs.create_plan(
                plan_id=plan_id, tenant_id=actor.tenant_id, lot_id=lot_id, code=code, name=name, is_default=is_default
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="TARIFF_PLAN_CREATED",
                    entity_type="tariff_plan",
                    entity_id=plan_id,
                    lot_id=lot_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"code": code},
                )
            )
            plan = await repos.tariffs.get_plan(plan_id)
        assert plan is not None
        return plan

    async def list_versions(self, actor: CurrentUser, plan_id: UUID) -> list[TariffVersionView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.tariffs.get_plan(plan_id) is None:
                raise NotFoundError("plan not found")
            return await repos.tariffs.list_versions(plan_id)

    async def publish_version(
        self, actor: CurrentUser, cmd: PublishVersionCommand, *, reason: str
    ) -> TariffVersionView:
        """Новая версия: предыдущая открытая закрывается в valid_from; пересечения исключает EXCLUDE (I-12).
        Правила валидируются доменом до записи; изменение прошлых версий невозможно — только новая версия."""
        rules = TariffRules.from_json(cmd.rules)
        if cmd.valid_to is not None and cmd.valid_to <= cmd.valid_from:
            raise ValidationError("valid_to must be after valid_from")
        version_id = uuid4()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            plan = await repos.tariffs.get_plan(cmd.plan_id)
            if plan is None:
                raise NotFoundError("plan not found")
            await repos.tariffs.close_open_version(cmd.plan_id, cmd.valid_from)
            number = await repos.tariffs.add_version(
                version_id=version_id,
                tenant_id=actor.tenant_id,
                plan_id=cmd.plan_id,
                rules=rules.to_json(),
                valid_from=cmd.valid_from,
                valid_to=cmd.valid_to,
                holiday_calendar_id=cmd.holiday_calendar_id,
                created_by=actor.user_id,
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="TARIFF_VERSION_PUBLISHED",
                    entity_type="tariff_plan_version",
                    entity_id=version_id,
                    lot_id=plan.lot_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"plan_id": str(cmd.plan_id), "version": number, "valid_from": cmd.valid_from.isoformat()},
                )
            )
            uow.publish(
                IntegrationEvent(
                    type="tariff.version.published.v1",
                    tenant_id=actor.tenant_id,
                    aggregate_type="tariff_plan",
                    aggregate_id=cmd.plan_id,
                    payload={
                        "plan_id": str(cmd.plan_id),
                        "lot_id": str(plan.lot_id),
                        "version_id": str(version_id),
                        "version": number,
                        "valid_from": cmd.valid_from.isoformat(),
                    },
                )
            )
            versions = await repos.tariffs.list_versions(cmd.plan_id)
        return next(v for v in versions if v.id == version_id)
