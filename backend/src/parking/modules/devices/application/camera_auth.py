"""Аутентификация камеры по API-ключу (фаза 4 §1): префикс → кандидаты → HMAC-SHA256 с pepper → compare_digest.

Почему не argon2/bcrypt: ключ генерирует система, 256 бит энтропии — перебор невозможен, растягивание не нужно,
а при 200 RPS argon2 (≈250 мс) убил бы p95. Неверный и несуществующий ключ неразличимы: один и тот же путь
(кеш кандидатов, включая отрицательный результат, → HMAC входа → compare_digest с каждым кандидатом или
с фиктивным дайджестом), один и тот же ответ 401. last_used_at — в Redis, флаш батчем (CORE §3).
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from parking.modules.devices.application.ports import CameraAuthCache, CameraKeyLookup, KeyCandidate
from parking.modules.devices.domain.camera import split_prefix
from parking.platform import metrics
from parking.platform.crypto import hmac_camera_key
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import AuthenticationError, ServiceUnavailableError

log = get_logger("devices.camera_auth")

_DUMMY_DIGEST = bytes(32)
CACHE_TTL_SECONDS = 60
ABUSE_WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class CameraPrincipal:
    key_id: UUID
    tenant_id: UUID
    camera_id: UUID
    lot_id: UUID
    code: str
    direction: str
    confidence_min: int
    confidence_review_min: int
    max_clock_skew_seconds: int
    late_event_window_hours: int


class CameraAuthService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        lookup: Callable[[UnitOfWork], CameraKeyLookup],
        cache: CameraAuthCache,
        pepper: bytes,
        clock: Clock,
        abuse_threshold: int = 20,
        on_abuse: Callable[[str, int], None] | None = None,
        is_connectivity_error: Callable[[BaseException], bool] = lambda _exc: False,
    ) -> None:
        self._uow_factory = uow_factory
        self._lookup = lookup
        self._cache = cache
        self._pepper = pepper
        self._clock = clock
        self._abuse_threshold = abuse_threshold
        self._on_abuse = on_abuse
        self._is_connectivity_error = is_connectivity_error

    async def authenticate(self, raw_key: str | None, *, client_ip: str | None) -> CameraPrincipal:
        started = time.perf_counter()
        try:
            return await self._authenticate(raw_key)
        except AuthenticationError:
            metrics.camera_auth_failed_total.inc()
            await self._count_abuse(client_ip)
            raise
        finally:
            metrics.camera_auth_seconds.observe(time.perf_counter() - started)

    async def _authenticate(self, raw_key: str | None) -> CameraPrincipal:
        prefix = split_prefix(raw_key or "")
        if prefix is None:
            raise AuthenticationError("invalid camera key")
        candidates = await self._candidates(prefix)
        digest = hmac_camera_key(self._pepper, raw_key or "")
        matched: KeyCandidate | None = None
        if not candidates:  # нет кандидатов — всё равно одно сравнение, чтобы путь по времени совпадал
            hmac.compare_digest(digest, _DUMMY_DIGEST)
        for candidate in candidates:
            if hmac.compare_digest(digest, candidate.key_hmac):
                matched = candidate
        if matched is None:
            raise AuthenticationError("invalid camera key")
        now = self._clock.now()
        if (matched.expires_at is not None and matched.expires_at <= now) or not matched.tenant_active:
            raise AuthenticationError("invalid camera key")
        try:
            await self._cache.touch(matched.key_id, now)
        except Exception as exc:
            log.warning("camera_auth_touch_failed", error_type=type(exc).__name__)
        return CameraPrincipal(
            key_id=matched.key_id,
            tenant_id=matched.tenant_id,
            camera_id=matched.camera_id,
            lot_id=matched.lot_id,
            code=matched.code,
            direction=matched.direction,
            confidence_min=matched.confidence_min,
            confidence_review_min=matched.confidence_review_min,
            max_clock_skew_seconds=matched.max_clock_skew_seconds,
            late_event_window_hours=matched.late_event_window_hours,
        )

    async def _candidates(self, prefix: str) -> list[KeyCandidate]:
        cached: list[KeyCandidate] | None = None
        try:
            cached = await self._cache.get(prefix)
        except Exception as exc:
            log.warning("camera_auth_cache_unavailable", error_type=type(exc).__name__)
        if cached is not None:
            return [c for c in cached if c.camera_active]
        try:
            async with self._uow_factory(None) as uow:
                found = await self._lookup(uow).candidates(prefix)
        except Exception as exc:
            if not self._is_connectivity_error(exc):
                raise
            stale = await self._stale(prefix)  # оффлайн-режим: суточная копия кандидатов, иначе 503
            if stale is None:
                raise ServiceUnavailableError("camera key store is unavailable") from exc
            log.warning("camera_auth_stale_cache", prefix_len=len(prefix))
            return [c for c in stale if c.camera_active]
        try:
            await self._cache.set(prefix, found, CACHE_TTL_SECONDS)  # отрицательный результат кешируется тоже
        except Exception as exc:
            log.warning("camera_auth_cache_unavailable", error_type=type(exc).__name__)
        return [c for c in found if c.camera_active]

    async def _stale(self, prefix: str) -> list[KeyCandidate] | None:
        try:
            return await self._cache.get_stale(prefix)
        except Exception as exc:
            log.warning("camera_auth_cache_unavailable", error_type=type(exc).__name__)
            return None

    async def invalidate(self, prefix: str) -> None:
        await self._cache.invalidate(prefix)

    async def _count_abuse(self, client_ip: str | None) -> None:
        if not client_ip:
            return
        try:
            failures = await self._cache.count_failure(client_ip, ABUSE_WINDOW_SECONDS)
        except Exception as exc:
            log.warning("camera_auth_cache_unavailable", error_type=type(exc).__name__)
            return
        if failures == self._abuse_threshold:
            metrics.camera_auth_abuse_total.inc()
            log.error("camera_auth_abuse", client_ip=client_ip, failures=failures, window_seconds=ABUSE_WINDOW_SECONDS)
            if self._on_abuse is not None:
                self._on_abuse(client_ip, failures)


def ttl_for(expires_at: datetime | None, now: datetime) -> int:
    if expires_at is None:
        return CACHE_TTL_SECONDS
    return max(1, min(CACHE_TTL_SECONDS, int((expires_at - now).total_seconds())))
