"""Правила доступа и абонементы: запись + аудит + событие в одной транзакции, инвалидация кеша после коммита."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from parking.modules.access.application.contracts import AccessRuleView, SubscriptionView
from parking.modules.access.application.ports import AccessRuleRepository, DecisionCache, SubscriptionRepository
from parking.modules.access.application.resolver import LocalDecisionCache
from parking.modules.access.domain.rules import AccessKind, AccessRule
from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.platform.crypto import PlateCrypto
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import NotFoundError, ValidationError
from parking.shared.plate import PlateNumber

log = get_logger("access.admin")


@dataclass(frozen=True, slots=True)
class AccessRepositories:
    rules: AccessRuleRepository
    audit: AuditWriter
    subscriptions: SubscriptionRepository


@dataclass(frozen=True, slots=True)
class AddRuleCommand:
    plate: str
    kind: AccessKind
    lot_id: UUID | None
    discount_percent: int = 0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class CreateSubscriptionCommand:
    plate: str
    lot_id: UUID | None
    plan_code: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int


class AccessAdminService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], AccessRepositories],
        crypto: PlateCrypto,
        cache: DecisionCache,
        local: LocalDecisionCache,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._crypto = crypto
        self._cache = cache
        self._local = local
        self._clock = clock

    async def add_rule(self, actor: CurrentUser, cmd: AddRuleCommand, *, reason: str) -> AccessRuleView:
        if cmd.kind is AccessKind.SUBSCRIPTION:
            raise ValidationError("subscription rules are created via create_subscription")
        plate = PlateNumber.parse(cmd.plate)
        now = self._clock.now()
        # VIP — всегда 100 %, blacklist — всегда 0: клиент не задаёт скидку там, где её нет
        discount = {AccessKind.VIP: 100, AccessKind.BLACKLIST: 0}.get(cmd.kind, cmd.discount_percent)
        rule = AccessRule(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            lot_id=cmd.lot_id,
            kind=cmd.kind,
            plate_hash=self._crypto.hash(actor.tenant_id, plate),
            valid_from=cmd.valid_from or now,
            valid_to=cmd.valid_to,
            discount_percent=discount,
            reason=cmd.note,
        )
        encrypted, version = self._crypto.encrypt(actor.tenant_id, plate)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await repos.rules.add(rule, encrypted, version, actor.user_id)
            await self._audit(repos, actor, "ACCESS_RULE_ADDED", rule, reason)
            uow.publish(self._changed_event(rule, "added"))
            self._schedule_invalidation(uow, actor.tenant_id, rule.plate_hash)
        return self._view(rule, plate.value, now)

    async def revoke_rule(self, actor: CurrentUser, rule_id: UUID, *, reason: str) -> None:
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            rule = await repos.rules.get(rule_id)
            if rule is None:
                raise NotFoundError("rule not found")
            if not await repos.rules.revoke(rule_id, revoked_by=actor.user_id, reason=reason, at=now):
                raise NotFoundError("rule not found")
            await self._audit(repos, actor, "ACCESS_RULE_REVOKED", rule, reason)
            uow.publish(self._changed_event(rule, "revoked"))
            self._schedule_invalidation(uow, actor.tenant_id, rule.plate_hash)

    async def create_subscription(
        self, actor: CurrentUser, cmd: CreateSubscriptionCommand, *, reason: str
    ) -> AccessRuleView:
        if cmd.ends_at <= cmd.starts_at:
            raise ValidationError("ends_at must be after starts_at")
        if cmd.price_minor < 0:
            raise ValidationError("price must be non-negative")
        plate = PlateNumber.parse(cmd.plate)
        plate_hash = self._crypto.hash(actor.tenant_id, plate)
        subscription_id = uuid4()
        rule = AccessRule(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            lot_id=cmd.lot_id,
            kind=AccessKind.SUBSCRIPTION,
            plate_hash=plate_hash,
            valid_from=cmd.starts_at,
            valid_to=cmd.ends_at,
            discount_percent=100,
            source_id=subscription_id,
            reason=f"subscription {cmd.plan_code}",
        )
        encrypted, version = self._crypto.encrypt(actor.tenant_id, plate)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await repos.subscriptions.add(
                subscription_id=subscription_id,
                tenant_id=actor.tenant_id,
                lot_id=cmd.lot_id,
                plate_hash=plate_hash,
                plan_code=cmd.plan_code,
                starts_at=cmd.starts_at,
                ends_at=cmd.ends_at,
                price_minor=cmd.price_minor,
                created_by=actor.user_id,
            )
            await repos.rules.add(rule, encrypted, version, actor.user_id)
            await self._audit(repos, actor, "SUBSCRIPTION_CREATED", rule, reason)
            uow.publish(self._changed_event(rule, "added"))
            self._schedule_invalidation(uow, actor.tenant_id, plate_hash)
        return self._view(rule, plate.value, self._clock.now())

    async def subscription(self, actor: CurrentUser, subscription_id: UUID) -> SubscriptionView:
        """Проданный абонемент — для чека: номер машины, срок, сумма."""
        async with self._uow_factory(actor.tenant_id) as uow:
            record = await self._repos(uow).subscriptions.get(subscription_id)
        allowed = actor.allowed_lots()
        if record is None or (record.lot_id is not None and allowed is not None and record.lot_id not in allowed):
            raise NotFoundError("subscription not found")
        plate = self._crypto.decrypt(actor.tenant_id, record.plate_encrypted, record.enc_key_version)
        return SubscriptionView(
            id=record.id,
            lot_id=record.lot_id,
            plate=plate.value,
            plan_code=record.plan_code,
            starts_at=record.starts_at,
            ends_at=record.ends_at,
            price_minor=record.price_minor,
        )

    async def list_rules(
        self,
        actor: CurrentUser,
        *,
        kind: AccessKind | None,
        lot_id: UUID | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[AccessRuleView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            rows = await self._repos(uow).rules.list(kind=kind, lot_id=lot_id, limit=limit, after=after)
        views = []
        for rule, encrypted, version, created_at in rows:
            try:
                plate_value = self._crypto.decrypt(actor.tenant_id, encrypted, version).value
            except Exception as exc:
                log.warning("access_rule_plate_undecryptable", rule_id=str(rule.id), error_type=type(exc).__name__)
                plate_value = "<undecryptable>"
            views.append(self._view(rule, plate_value, created_at))
        return views

    def _schedule_invalidation(self, uow: UnitOfWork, tenant_id: UUID, plate_hash: bytes) -> None:
        async def invalidate() -> None:
            self._local.invalidate(tenant_id, plate_hash)
            await self._cache.invalidate(tenant_id, plate_hash)

        uow.after_commit(invalidate)

    @staticmethod
    def _changed_event(rule: AccessRule, change: str) -> IntegrationEvent:
        return IntegrationEvent(
            type="access.rule.changed.v1",
            tenant_id=rule.tenant_id,
            aggregate_type="access_rule",
            aggregate_id=rule.id,
            payload={
                "rule_id": str(rule.id),
                "kind": rule.kind.value,
                "lot_id": str(rule.lot_id) if rule.lot_id else None,
                "change": change,
                "plate_hash": rule.plate_hash.hex(),
            },
        )

    @staticmethod
    async def _audit(repos: AccessRepositories, actor: CurrentUser, action: str, rule: AccessRule, reason: str) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="access_rule",
                entity_id=rule.id,
                lot_id=rule.lot_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload={
                    "kind": rule.kind.value,
                    "discount_percent": rule.discount_percent,
                    "plate_hash": rule.plate_hash.hex(),
                },
            )
        )

    @staticmethod
    def _view(rule: AccessRule, plate: str, created_at: datetime) -> AccessRuleView:
        return AccessRuleView(
            id=rule.id,
            lot_id=rule.lot_id,
            kind=rule.kind,
            plate=plate,
            valid_from=rule.valid_from,
            valid_to=rule.valid_to,
            discount_percent=rule.discount_percent,
            source_id=rule.source_id,
            reason=rule.reason,
            revoked_at=rule.revoked_at,
            created_at=created_at,
        )
