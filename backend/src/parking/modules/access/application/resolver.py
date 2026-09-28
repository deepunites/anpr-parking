"""AccessResolver с двумя уровнями кеша (ADR-04): локальный (5 с) → Redis (≤ 300 с) → PostgreSQL (один запрос)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from parking.modules.access.application.ports import AccessRuleRepository, DecisionCache
from parking.modules.access.domain.rules import AccessDecision, decide
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.platform.tracing import span
from parking.shared.clock import Clock

log = get_logger("access.resolver")

LOCAL_TTL_SECONDS = 5.0
REDIS_TTL_SECONDS = 300


class LocalDecisionCache:
    """Кеш процесса: TTL 5 с — заявленная верхняя граница вступления blacklist в силу (N = 5 с)."""

    def __init__(
        self, ttl_seconds: float = LOCAL_TTL_SECONDS, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._now = monotonic
        self._items: dict[tuple[UUID, UUID, bytes], tuple[float, AccessDecision]] = {}

    def get(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes) -> AccessDecision | None:
        item = self._items.get((tenant_id, lot_id, plate_hash))
        if item is None:
            return None
        expires, decision = item
        if expires <= self._now():
            del self._items[(tenant_id, lot_id, plate_hash)]
            return None
        return decision

    def set(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, decision: AccessDecision) -> None:
        if len(self._items) > 50_000:
            self._items.clear()
        self._items[(tenant_id, lot_id, plate_hash)] = (self._now() + self._ttl, decision)

    def invalidate(self, tenant_id: UUID, plate_hash: bytes) -> None:
        for key in [k for k in self._items if k[0] == tenant_id and k[2] == plate_hash]:
            del self._items[key]


class CachedAccessResolver:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repository: Callable[[UnitOfWork], AccessRuleRepository],
        cache: DecisionCache,
        local: LocalDecisionCache,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._repository = repository
        self._cache = cache
        self._local = local
        self._clock = clock

    async def resolve(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime) -> AccessDecision:
        return await self._resolve(None, tenant_id, lot_id, plate_hash, at)

    async def resolve_in(
        self, uow: UnitOfWork, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime
    ) -> AccessDecision:
        """То же, но промах кешей читает правила в транзакции вызывающего (camera-событие: без второго соединения,
        BEGIN/set_config/COMMIT — правила и сессия в одной транзакции)."""
        return await self._resolve(uow, tenant_id, lot_id, plate_hash, at)

    async def _resolve(
        self, uow: UnitOfWork | None, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime
    ) -> AccessDecision:
        with span("access.resolve", tenant_id=tenant_id, lot_id=lot_id) as current:
            decision = await self._resolve_traced(uow, tenant_id, lot_id, plate_hash, at)
            current.set_attribute("access.kind", decision.kind.value)
            metrics.access_decision_total.labels(kind=decision.kind.value).inc()
            return decision

    async def _resolve_traced(
        self, uow: UnitOfWork | None, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime
    ) -> AccessDecision:
        cached = self._local.get(tenant_id, lot_id, plate_hash)
        if cached is not None and _still_valid(cached, at):
            return cached
        try:
            remote = await self._cache.get(tenant_id, lot_id, plate_hash)
        except Exception as exc:
            log.warning("access_cache_unavailable", error_type=type(exc).__name__)
            remote = None
        if remote is not None and _still_valid(remote, at):
            self._local.set(tenant_id, lot_id, plate_hash, remote)
            return remote
        if uow is None:
            decision = await self._from_database(tenant_id, lot_id, plate_hash, at)
        else:
            decision = decide(await self._repository(uow).rules_for(plate_hash, lot_id, at), lot_id, at)
        self._local.set(tenant_id, lot_id, plate_hash, decision)
        try:
            await self._cache.set(tenant_id, lot_id, plate_hash, decision, _ttl_for(decision, at))
        except Exception as exc:
            log.warning("access_cache_unavailable", error_type=type(exc).__name__)
        return decision

    async def cached(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes) -> AccessDecision | None:
        """Только кеши (локальный, затем Redis) — оффлайн-режим, когда PostgreSQL недоступен (A-10)."""
        local = self._local.get(tenant_id, lot_id, plate_hash)
        if local is not None:
            return local
        try:
            return await self._cache.get(tenant_id, lot_id, plate_hash)
        except Exception as exc:
            log.warning("access_cache_unavailable", error_type=type(exc).__name__)
            return None

    async def resolve_uncached(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime) -> AccessDecision:
        return await self._from_database(tenant_id, lot_id, plate_hash, at)

    async def _from_database(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime) -> AccessDecision:
        async with self._uow_factory(tenant_id) as uow:
            rules = await self._repository(uow).rules_for(plate_hash, lot_id, at)
        return decide(rules, lot_id, at)


def _still_valid(decision: AccessDecision, at: datetime) -> bool:
    return decision.valid_until is None or at < decision.valid_until


def _ttl_for(decision: AccessDecision, at: datetime) -> int:
    if decision.valid_until is None:
        return REDIS_TTL_SECONDS
    remaining = int((decision.valid_until - at).total_seconds())
    return max(1, min(REDIS_TTL_SECONDS, remaining))
