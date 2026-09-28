"""Media (фаза 4 §5): загрузка изображений прямо в MinIO по presigned URL (мимо API), просмотр по presigned GET
с проверкой tenant, ключ объекта — жёстко из ключа камеры, размер и Content-Type — в presigned-политике."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from parking.modules.devices.application.contracts import CameraPrincipal
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.media.application.ports import (
    MediaRepository,
    ObjectStore,
    PendingFrames,
    PresignedUpload,
    SessionImageSource,
)
from parking.modules.media.domain.object_key import build_key, key_belongs_to, parse_key
from parking.modules.parking.application.contracts import SessionView
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import NotFoundError, ValidationError

log = get_logger("media")


@dataclass(frozen=True, slots=True)
class SessionImage:
    event_id: UUID
    object_key: str
    event_time: datetime
    url: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class MediaRepositories:
    media: MediaRepository
    events: SessionImageSource


class MediaService:
    def __init__(
        self,
        *,
        store: ObjectStore,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], MediaRepositories],
        session_lookup: Callable[[CurrentUser, UUID], Awaitable[SessionView]],
        clock: Clock,
        max_bytes: int,
        put_ttl_seconds: int,
        get_ttl_seconds: int,
        pending: PendingFrames | None = None,
        pending_ttl_seconds: int = 6 * 3600,
    ) -> None:
        self._store = store
        self._pending = pending
        self._pending_ttl = pending_ttl_seconds
        self._uow_factory = uow_factory
        self._repos = repos
        self._session_lookup = session_lookup
        self._clock = clock
        self._max_bytes = max_bytes
        self._put_ttl = put_ttl_seconds
        self._get_ttl = get_ttl_seconds

    async def presign_upload(self, principal: CameraPrincipal) -> PresignedUpload:
        """Камера получает URL и ключ; ключ навязан сервером (tenant/lot/camera из ключа камеры)."""
        key = build_key(principal.tenant_id, principal.lot_id, principal.camera_id, self._clock.now(), uuid4())
        presigned = await self._store.presign_upload(key, max_bytes=self._max_bytes, ttl_seconds=self._put_ttl)
        try:
            async with self._uow_factory(principal.tenant_id) as uow:
                await self._repos(uow).media.add_expected(
                    key=key,
                    tenant_id=principal.tenant_id,
                    lot_id=principal.lot_id,
                    camera_id=principal.camera_id,
                    at=self._clock.now(),
                )
        except Exception as exc:  # БД недоступна — presign всё равно отдаём: изображение важнее учёта
            log.warning("media_expected_not_recorded", error_type=type(exc).__name__)
        return presigned

    async def store_frame(self, principal: CameraPrincipal, data: bytes) -> str:
        """Vendor-адаптеры (Hikvision/Dahua шлют кадр в теле): API кладёт объект сам, ключ — по тем же правилам."""
        if not data or len(data) > self._max_bytes:
            raise ValidationError("image is empty or exceeds the size limit")
        if not data.startswith(b"\xff\xd8\xff"):
            raise ValidationError("image must be JPEG")
        key = build_key(principal.tenant_id, principal.lot_id, principal.camera_id, self._clock.now(), uuid4())
        try:
            await self._store.put_object(key, data)
        except Exception as exc:
            # хранилище недоступно: событие важнее кадра — ключ выдаём, байты ждут повтора (§5 фазы 8)
            if self._pending is None:
                raise
            await self._pending.put(key, data, ttl_seconds=self._pending_ttl)
            metrics.media_store_failed_total.inc()
            log.warning("media_store_deferred", error_type=type(exc).__name__, pending=await self._pending.length())
        return key

    async def retry_pending(self, *, limit: int = 100) -> tuple[int, int]:
        """Джоб: повторная отправка отложенных кадров; при новом отказе кадр возвращается в очередь.
        Возвращает (отправлено, отложено снова)."""
        if self._pending is None:
            return 0, 0
        sent = deferred = 0
        for key, data in await self._pending.pop_batch(limit):
            try:
                await self._store.put_object(key, data)
                sent += 1
            except Exception as exc:
                await self._pending.requeue(key, data, ttl_seconds=self._pending_ttl)
                deferred += 1
                log.warning("media_retry_failed", error_type=type(exc).__name__)
                break  # хранилище всё ещё недоступно — не молотить очередь впустую
        if sent:
            log.info("media_pending_retried", sent=sent, deferred=deferred)
        metrics.media_pending_frames.set(await self._pending.length())
        return sent, deferred

    def key_belongs_to(self, key: str, tenant_id: UUID, lot_id: UUID, camera_id: UUID) -> bool:
        return key_belongs_to(key, tenant_id, lot_id, camera_id)

    async def session_images(self, actor: CurrentUser, session_id: UUID) -> list[SessionImage]:
        session = await self._session_lookup(actor, session_id)  # 404, если сессия чужая или вне скоупа площадок
        async with self._uow_factory(actor.tenant_id) as uow:
            rows = await self._repos(uow).events.images_for_session(session.id)
        images: list[SessionImage] = []
        for event_id, key, event_time in rows:
            parts = parse_key(key)
            if parts is None or parts.tenant_id != actor.tenant_id:
                log.error("media_key_tenant_mismatch", event_id=str(event_id))
                continue
            url = await self._store.presign_get(key, ttl_seconds=self._get_ttl)
            images.append(SessionImage(event_id, key, event_time, url, self._get_ttl))
        if not rows:
            raise NotFoundError("session has no images")
        return images
