"""ANPR ingest (фаза 4 §1, §4): конвейер от дешёвых проверок к дорогим, одна транзакция на событие,
оффлайн-буфер в Redis Stream по circuit breaker пула БД.

Порядок: rate limit → направление → время (skew помечаем, replay отклоняем) → номер → image_object_key →
уверенность → идемпотентность (Redis-ускоритель, истина — anpr_idempotency) → решение доступа → одна UoW:
advisory-lock по ключу, событие, сессия/сага, снимок ответа. Ответ — после коммита, не дожидаясь шлагбаума.

Rate limit 120/мин на ключ: самый загруженный въезд даёт ~1 машину в 3 с (20 событий/мин), с повторами
и дублями камеры ×2 — 40; порог 120 оставляет трёхкратный запас и режет только шторм (петля ретраев прошивки,
украденный ключ). Сброс буфера камеры после обрыва растягивается на минуты — такие события и так late.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID, uuid4

from parking.modules.access.application.contracts import AccessDecision, AccessKind
from parking.modules.anpr.application.ports import (
    AnprEventRepository,
    BarrierInfoCache,
    CachedAccess,
    IdempotencyRepository,
    OfflineBuffer,
    RateLimiter,
    ResponseCache,
    StoredEvent,
)
from parking.modules.anpr.domain.event import (
    CameraEvent,
    ConfidenceClass,
    EventType,
    RejectReason,
    TimeClass,
    classify_confidence,
    classify_time,
    direction_for,
    event_second,
    idempotency_key,
)
from parking.modules.devices.application.contracts import CameraPrincipal, DriverRegistry, HeartbeatStore
from parking.modules.parking.application.contracts import (
    EntryCommand,
    EntryStatus,
    ExitCommand,
    ExitStatus,
    ParkingCommands,
)
from parking.platform import metrics
from parking.platform.breaker import CircuitBreaker, CircuitOpenError
from parking.platform.crypto import PlateCrypto
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.platform.tracing import mark_exit_flow
from parking.shared.clock import Clock
from parking.shared.correlation import bind_log_context, get_correlation_id
from parking.shared.errors import RateLimitedError, ServiceUnavailableError, ValidationError
from parking.shared.plate import PlateNumber

log = get_logger("anpr.ingest")

RESPONSE_CACHE_TTL = 24 * 3600
REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True, kw_only=True)
class CameraEventInput:
    camera_code: str
    event_type: str
    plate_number: str
    confidence: int
    event_time: datetime
    image_object_key: str | None = None
    direction: str | None = None
    recognition: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestResponse:
    status: str
    correlation_id: str
    session_id: UUID | None = None
    barrier_command_id: UUID | None = None
    saga_id: UUID | None = None
    event_id: UUID | None = None
    duplicate: bool = False
    queued: bool = False

    @property
    def http_status(self) -> int:
        return 202 if self.queued else 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": str(self.session_id) if self.session_id else None,
            "barrier_command_id": str(self.barrier_command_id) if self.barrier_command_id else None,
            "saga_id": str(self.saga_id) if self.saga_id else None,
            "event_id": str(self.event_id) if self.event_id else None,
            "correlation_id": self.correlation_id,
            "duplicate": self.duplicate,
            "queued": self.queued,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, duplicate: bool) -> IngestResponse:
        return cls(
            status=str(d["status"]),
            correlation_id=str(d.get("correlation_id") or get_correlation_id()),
            session_id=UUID(d["session_id"]) if d.get("session_id") else None,
            barrier_command_id=UUID(d["barrier_command_id"]) if d.get("barrier_command_id") else None,
            saga_id=UUID(d["saga_id"]) if d.get("saga_id") else None,
            event_id=UUID(d["event_id"]) if d.get("event_id") else None,
            duplicate=duplicate,
        )


@dataclass(frozen=True, slots=True)
class IngestRepositories:
    events: AnprEventRepository
    idempotency: IdempotencyRepository


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestPolicy:
    rate_limit_per_minute: int = 120
    late_after: timedelta = timedelta(seconds=600)
    offline_maxlen: int = 1_000_000
    offline_alarm_ratio: float = 0.8


def _is_builtin_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and _is_builtin_timeout(cause)


class IngestService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], IngestRepositories],
        parking: ParkingCommands,
        access: CachedAccess,
        crypto: PlateCrypto,
        rate_limiter: RateLimiter,
        response_cache: ResponseCache,
        offline: OfflineBuffer,
        barrier_cache: BarrierInfoCache,
        drivers: DriverRegistry,
        heartbeats: HeartbeatStore,
        db_breaker: CircuitBreaker,
        is_connectivity_error: Callable[[BaseException], bool],
        is_timeout_error: Callable[[BaseException], bool] = _is_builtin_timeout,
        image_key_ok: Callable[[str, UUID, UUID, UUID], bool],
        clock: Clock,
        policy: IngestPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._parking = parking
        self._access = access
        self._crypto = crypto
        self._rate_limiter = rate_limiter
        self._responses = response_cache
        self._offline = offline
        self._barrier_cache = barrier_cache
        self._drivers = drivers
        self._heartbeats = heartbeats
        self._db_breaker = db_breaker
        self._is_connectivity_error = is_connectivity_error
        self._is_timeout_error = is_timeout_error
        self._image_key_ok = image_key_ok
        self._clock = clock
        self._policy = policy or IngestPolicy()

    # ---------- приём ----------

    async def process(self, principal: CameraPrincipal, data: CameraEventInput) -> IngestResponse:
        bind_log_context(lot_id=principal.lot_id, camera_id=principal.camera_id, tenant_id=principal.tenant_id)
        await self._rate_limit(principal)
        event = self._validate(principal, data)
        if event.event_type is EventType.EXIT:
            mark_exit_flow()  # трасса EXIT экспортируется целиком (фаза 7 §3)
        if event.confidence_class is ConfidenceClass.REJECT:
            await self._record_rejected(event, RejectReason.LOW_CONFIDENCE)
            _reject(RejectReason.LOW_CONFIDENCE, "confidence below review threshold; image retained")
        key = self._key(event)
        cached = await self._cached_response(key)
        if cached is not None:
            metrics.camera_event_total.labels(result="duplicate").inc()
            return cached
        if self._db_breaker.is_open:
            return await self._enqueue_offline(event, key, breaker_open=True)
        try:
            response = await self._commit(event, key, late=event.time.late)
        except Exception as exc:
            if not self._is_connectivity_error(exc):
                raise
            if self._is_timeout_error(exc):
                self._db_breaker.trip()  # таймаут = сеть «в чёрной дыре»: сразу в оффлайн-режим, без ещё N ожиданий
            else:
                self._db_breaker.record_failure()
            log.error("anpr_db_unavailable", error_type=type(exc).__name__, breaker=self._db_breaker.state.value)
            return await self._enqueue_offline(event, key, breaker_open=False)
        self._db_breaker.record_success()
        metrics.offline_mode_active.set(0)
        return response

    def check(self, principal: CameraPrincipal, data: CameraEventInput) -> CameraEvent:
        """v2 этап 6, песочница: те же проверки, что у приёма (направление, время, номер, уверенность), без записи
        и без лимита частоты. Бросает ValidationError с кодом причины — ровно то, что получила бы камера."""
        event = self._validate(principal, data)
        if event.confidence_class is ConfidenceClass.REJECT:
            _reject(RejectReason.LOW_CONFIDENCE, "confidence below review threshold; image retained")
        return event

    async def process_buffered(self, fields: dict[str, str]) -> IngestResponse:
        """Replay из оффлайн-буфера: тот же коммит, late=True (сессия задним числом, шлагбаум не трогать —
        он уже открыт на месте или машина давно уехала), queued_offline=true в anpr_event."""
        event, key = self._from_envelope(fields)
        try:
            self._db_breaker.acquire()
        except CircuitOpenError as exc:
            raise ServiceUnavailableError("database circuit is open") from exc
        try:
            response = await self._commit(event, key, late=True)
        except Exception as exc:
            if self._is_connectivity_error(exc):
                self._db_breaker.record_failure()
            raise
        self._db_breaker.record_success()
        metrics.offline_mode_active.set(0)
        return response

    # ---------- шаги ----------

    async def _rate_limit(self, principal: CameraPrincipal) -> None:
        try:
            wait = await self._rate_limiter.hit(
                f"cam:{principal.key_id}", limit=self._policy.rate_limit_per_minute, window_seconds=60
            )
        except Exception as exc:  # Redis недоступен — лимит не применяем, но не роняем приём
            log.warning("rate_limiter_unavailable", error_type=type(exc).__name__)
            return
        if wait is not None:
            metrics.camera_event_rejected_total.labels(reason=RejectReason.RATE_LIMITED.value).inc()
            raise RateLimitedError("camera event rate limit exceeded", retry_after_seconds=wait)

    def _validate(self, principal: CameraPrincipal, data: CameraEventInput) -> CameraEvent:
        try:
            event_type = EventType(data.event_type)
        except ValueError:
            _reject(RejectReason.DIRECTION_MISMATCH, "event_type must be ENTRY or EXIT")
        expected_direction = direction_for(event_type)
        if principal.direction != expected_direction or (data.direction and data.direction != expected_direction):
            _reject(RejectReason.DIRECTION_MISMATCH, "event direction does not match camera direction")
        if data.camera_code != principal.code:
            _reject(RejectReason.DIRECTION_MISMATCH, "camera_code does not match the key's camera")
        now = self._clock.now()
        time_class = classify_time(
            data.event_time,
            now,
            max_skew_seconds=principal.max_clock_skew_seconds,
            late_after=self._policy.late_after,
            window=timedelta(hours=principal.late_event_window_hours),
        )
        metrics.camera_clock_skew_seconds.labels(lot=str(principal.lot_id)).observe(abs(time_class.skew_seconds))
        if time_class.skew_exceeded:
            log.warning(
                "camera_clock_skew",
                camera_id=str(principal.camera_id),
                skew_seconds=time_class.skew_seconds,
                max_skew_seconds=principal.max_clock_skew_seconds,
            )
        if time_class.replay:
            _reject(RejectReason.REPLAY, "event_time is outside the lot's late event window")
        try:
            plate = PlateNumber.parse(data.plate_number)
        except ValueError as exc:
            metrics.camera_event_rejected_total.labels(reason=RejectReason.INVALID_PLATE.value).inc()
            raise ValidationError(str(exc), code=RejectReason.INVALID_PLATE.value) from exc
        if data.image_object_key is not None and not self._image_key_ok(
            data.image_object_key, principal.tenant_id, principal.lot_id, principal.camera_id
        ):
            _reject(RejectReason.INVALID_IMAGE_KEY, "image_object_key does not belong to this camera")
        return CameraEvent(
            event_id=uuid4(),
            tenant_id=principal.tenant_id,
            lot_id=principal.lot_id,
            camera_id=principal.camera_id,
            camera_code=principal.code,
            event_type=event_type,
            plate=plate,
            confidence=data.confidence,
            event_time=data.event_time,
            received_at=now,
            image_object_key=data.image_object_key,
            recognition=dict(data.recognition or {}),
            correlation_id=get_correlation_id(),
            time=time_class,
            confidence_class=classify_confidence(
                data.confidence, minimum=principal.confidence_min, review_min=principal.confidence_review_min
            ),
        )

    def _key(self, event: CameraEvent) -> bytes:
        plate_hash = self._crypto.hash(event.tenant_id, event.plate)
        return idempotency_key(event.tenant_id, event.camera_id, event.event_type, plate_hash, event.event_time)

    async def _cached_response(self, key: bytes) -> IngestResponse | None:
        try:
            cached = await self._responses.get(key)
        except Exception as exc:
            log.warning("response_cache_unavailable", error_type=type(exc).__name__)
            return None
        return None if cached is None else IngestResponse.from_dict(cached, duplicate=True)

    async def _commit(self, event: CameraEvent, key: bytes, *, late: bool) -> IngestResponse:
        """Одна транзакция: advisory-lock по ключу (параллельные дубли ждут, не работают), проверка реестра, решение
        доступа (кеши → правила этой же транзакцией), сессия/сага, событие, снимок ответа. Никаких вложенных
        транзакций/второго соединения из пула внутри."""
        started = time.perf_counter()
        plate_hash = self._crypto.hash(event.tenant_id, event.plate)
        encrypted, enc_version = self._crypto.encrypt(event.tenant_id, event.plate)
        second = event_second(event.event_time)
        async with self._uow_factory(event.tenant_id) as uow:
            repos = self._repos(uow)
            # Сразу блокировка, затем одна проверка реестра: дубли после коммита оригинала ловит Redis-кеш ответов
            # (`_cached_response`), а предварительная проверка без блокировки стоила лишний SQL каждому новому событию.
            await repos.idempotency.lock(key)
            existing = await repos.idempotency.find(key, second)
            if existing is not None:
                metrics.camera_event_total.labels(result="duplicate").inc()
                return IngestResponse.from_dict(existing.response, duplicate=True)
            decision = await self._access.resolve_in(uow, event.tenant_id, event.lot_id, plate_hash, event.event_time)
            status, session_id, saga_id, command_id = await self._apply(uow, event, decision, late=late)
            response = IngestResponse(
                status=status,
                correlation_id=event.correlation_id,
                session_id=session_id,
                barrier_command_id=command_id,
                saga_id=saga_id,
                event_id=event.event_id,
            )
            await repos.events.add(
                StoredEvent(
                    event=event,
                    plate_hash=plate_hash,
                    plate_encrypted=encrypted,
                    hmac_key_version=enc_version,
                    enc_key_version=enc_version,
                    result=status,
                    reject_reason=None,
                    session_id=session_id,
                    saga_id=saga_id,
                    idempotency_key=key,
                    late=late,
                )
            )
            await repos.idempotency.add(
                key,
                event_second(event.event_time),
                tenant_id=event.tenant_id,
                camera_id=event.camera_id,
                event_id=event.event_id,
                result=status,
                response=response.to_dict(),
            )
            uow.after_commit(self._after_commit(event, key, response))
        metrics.camera_event_total.labels(result=status).inc()
        (metrics.entry_total if event.event_type is EventType.ENTRY else metrics.exit_total).labels(
            lot=str(event.lot_id), result=status
        ).inc()
        bind_log_context(session_id=session_id)
        log.info(
            "anpr_event_accepted",
            event_id=str(event.event_id),
            camera_id=str(event.camera_id),
            event_type=event.event_type.value,
            plate=event.plate.masked,
            result=status,
            late=late,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response

    async def _apply(
        self, uow: UnitOfWork, event: CameraEvent, decision: AccessDecision, *, late: bool
    ) -> tuple[str, UUID | None, UUID | None, UUID | None]:
        review = event.confidence_class is ConfidenceClass.REVIEW
        if event.event_type is EventType.ENTRY:
            entry = await self._parking.process_entry_in(
                uow,
                EntryCommand(
                    tenant_id=event.tenant_id,
                    lot_id=event.lot_id,
                    plate=event.plate,
                    event_time=event.event_time,
                    decision=decision,
                    camera_id=event.camera_id,
                    event_id=event.event_id,
                    confidence=event.confidence,
                    requires_review=review,
                    late=late,
                ),
            )
            return entry.status.value, entry.session_id, None, entry.barrier_command_id
        exit_ = await self._parking.start_exit_in(
            uow,
            ExitCommand(
                tenant_id=event.tenant_id,
                lot_id=event.lot_id,
                plate=event.plate,
                event_time=event.event_time,
                event_id=event.event_id,
                decision=decision,
                camera_id=event.camera_id,
                confidence=event.confidence,
                requires_review=review,
                late=late,
            ),
        )
        return exit_.status.value, exit_.session_id, exit_.saga_id, None

    def _after_commit(self, event: CameraEvent, key: bytes, response: IngestResponse) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            try:
                await self._responses.set(key, response.to_dict(), RESPONSE_CACHE_TTL)
                await self._heartbeats.note_event(event.camera_id, event.received_at)
            except Exception as exc:
                log.warning("anpr_after_commit_cache_failed", error_type=type(exc).__name__)

        return run

    async def _record_rejected(self, event: CameraEvent, reason: RejectReason) -> None:
        metrics.camera_event_rejected_total.labels(reason=reason.value).inc()
        plate_hash = self._crypto.hash(event.tenant_id, event.plate)
        encrypted, enc_version = self._crypto.encrypt(event.tenant_id, event.plate)
        try:
            async with self._uow_factory(event.tenant_id) as uow:
                await self._repos(uow).events.add(
                    StoredEvent(
                        event=event,
                        plate_hash=plate_hash,
                        plate_encrypted=encrypted,
                        hmac_key_version=enc_version,
                        enc_key_version=enc_version,
                        result=REJECTED,
                        reject_reason=reason.value,
                        session_id=None,
                        saga_id=None,
                        idempotency_key=self._key(event),
                        late=event.time.late,
                    )
                )
        except Exception as exc:
            if not self._is_connectivity_error(exc):
                raise
            log.warning("anpr_reject_not_recorded", error_type=type(exc).__name__)

    # ---------- оффлайн ----------

    async def _enqueue_offline(self, event: CameraEvent, key: bytes, *, breaker_open: bool) -> IngestResponse:
        metrics.offline_mode_active.set(1)
        try:
            length = await self._offline.length()
        except Exception as exc:
            raise ServiceUnavailableError("database and offline buffer are unavailable") from exc
        if length >= self._policy.offline_maxlen:
            log.error("offline_buffer_full", length=length, maxlen=self._policy.offline_maxlen)
            raise ServiceUnavailableError("offline buffer is full; event not accepted")
        if length >= int(self._policy.offline_maxlen * self._policy.offline_alarm_ratio):
            log.error("offline_buffer_near_capacity", length=length, maxlen=self._policy.offline_maxlen)
        barrier_opened = await self._open_offline(event)
        fields = self._envelope(event, key, barrier_opened=barrier_opened)
        try:
            entry_id = await self._offline.enqueue(fields)
        except Exception as exc:
            raise ServiceUnavailableError("offline buffer write failed; event not accepted") from exc
        metrics.offline_queue_length.set(length + 1)
        metrics.camera_event_total.labels(result="QUEUED").inc()
        log.warning(
            "anpr_event_queued_offline",
            event_id=str(event.event_id),
            entry_id=entry_id,
            breaker_open=breaker_open,
            barrier_opened=barrier_opened,
        )
        return IngestResponse(
            status="QUEUED", correlation_id=event.correlation_id, event_id=event.event_id, queued=True
        )

    async def _open_offline(self, event: CameraEvent) -> bool:
        """A-10: в оффлайне ENTRY и бесплатные EXIT открывают шлагбаум по кешу решения; платные EXIT — ручной режим.
        Blacklist из кеша — не открывать (I-8). Команды в БД нет (БД недоступна) — факт уходит в конверт и аудит
        replay."""
        if event.time.late or event.confidence_class is ConfidenceClass.REVIEW:
            return False
        plate_hash = self._crypto.hash(event.tenant_id, event.plate)
        decision = await self._access.cached(event.tenant_id, event.lot_id, plate_hash)
        if decision is not None and decision.kind is AccessKind.BLACKLIST:
            return False
        if event.event_type is EventType.EXIT and not (
            decision is not None
            and decision.kind in {AccessKind.SUBSCRIPTION, AccessKind.VIP, AccessKind.WHITELIST}
            and decision.discount_percent == 100
        ):
            return False
        try:
            barrier = await self._barrier_cache.for_camera(event.lot_id, event.camera_id)
            if barrier is None:
                return False
            result = await self._drivers.driver_for(barrier).open(barrier, event.event_id)
        except Exception as exc:
            log.error("offline_barrier_open_failed", error_type=type(exc).__name__)
            return False
        log.warning("offline_barrier_open", barrier_id=str(barrier.id), ok=result.ok, error=result.error)
        return result.ok

    @staticmethod
    def _envelope(event: CameraEvent, key: bytes, *, barrier_opened: bool) -> dict[str, str]:
        return {
            "event_id": str(event.event_id),
            "correlation_id": event.correlation_id,
            "received_at": event.received_at.isoformat(),
            "retry_count": str(event.retry_count),
            "tenant_id": str(event.tenant_id),
            "lot_id": str(event.lot_id),
            "camera_id": str(event.camera_id),
            "camera_code": event.camera_code,
            "idempotency_key": key.hex(),
            "event_type": event.event_type.value,
            "plate_number": event.plate.value,  # открытый номер: конверт живёт в Redis до replay, не в логах
            "confidence": str(event.confidence),
            "event_time": event.event_time.isoformat(),
            "image_object_key": event.image_object_key or "",
            "recognition": json.dumps(event.recognition, default=str),
            "skew_seconds": str(event.time.skew_seconds),
            "late": "1" if event.time.late else "0",
            "confidence_class": event.confidence_class.value,
            "barrier_opened_offline": "1" if barrier_opened else "0",
        }

    @staticmethod
    def _from_envelope(f: dict[str, str]) -> tuple[CameraEvent, bytes]:
        event = CameraEvent(
            event_id=UUID(f["event_id"]),
            tenant_id=UUID(f["tenant_id"]),
            lot_id=UUID(f["lot_id"]),
            camera_id=UUID(f["camera_id"]),
            camera_code=f["camera_code"],
            event_type=EventType(f["event_type"]),
            plate=PlateNumber.parse(f["plate_number"]),
            confidence=int(f["confidence"]),
            event_time=datetime.fromisoformat(f["event_time"]),
            received_at=datetime.fromisoformat(f["received_at"]),
            image_object_key=f.get("image_object_key") or None,
            recognition=json.loads(f.get("recognition") or "{}"),
            correlation_id=f["correlation_id"],
            time=TimeClass(int(f.get("skew_seconds", "0")), False, f.get("late") == "1", False),
            confidence_class=ConfidenceClass(f.get("confidence_class", "NORMAL")),
            queued_offline=True,
            retry_count=int(f.get("retry_count", "0")),
        )
        return event, bytes.fromhex(f["idempotency_key"])


def _reject(reason: RejectReason, message: str) -> NoReturn:
    metrics.camera_event_rejected_total.labels(reason=reason.value).inc()
    raise ValidationError(message, code=reason.value)


__all__ = [
    "CameraEventInput",
    "EntryStatus",
    "ExitStatus",
    "IngestPolicy",
    "IngestRepositories",
    "IngestResponse",
    "IngestService",
]
