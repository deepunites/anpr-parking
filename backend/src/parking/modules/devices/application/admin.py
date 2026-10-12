"""Администрирование устройств: камеры, ключи камер (выпуск/ротация/отзыв), шлагбаумы (SSRF-проверка endpoint),
ручные команды с обязательным reason (I-15). Все изменения — с аудитом в той же транзакции."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.devices.application.commands import DeviceRepositories, SqlBarrierCommands
from parking.modules.devices.application.contracts import BarrierCommandResult
from parking.modules.devices.application.ports import (
    BarrierRepository,
    CameraAuthCache,
    CameraKeyRepository,
    CameraRepository,
    CredentialBox,
    DriverRegistry,
    DriverResult,
    EndpointResolver,
)
from parking.modules.devices.domain.barrier import Barrier, BarrierDriverKind
from parking.modules.devices.domain.camera import (
    Camera,
    CameraApiKey,
    CameraDirection,
    CameraVendor,
    IssuedKey,
    generate_raw_key,
    split_prefix,
)
from parking.modules.devices.domain.command import BarrierCommand, CommandSource
from parking.modules.devices.domain.endpoint import check_addresses, parse_endpoint
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.tenancy.application.contracts import LotDirectory, SubscriptionGate
from parking.platform.crypto import hmac_camera_key
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import ConflictError, NotFoundError, ServiceUnavailableError, ValidationError

log = get_logger("devices.admin")

DEFAULT_ROTATION_GRACE = timedelta(hours=24)  # A-17


@dataclass(frozen=True, slots=True)
class AdminRepositories:
    cameras: CameraRepository
    keys: CameraKeyRepository
    barriers: BarrierRepository
    lots: LotDirectory
    audit: AuditWriter
    devices: DeviceRepositories


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCameraCommand:
    lot_id: UUID
    code: str
    name: str
    direction: CameraDirection
    vendor: CameraVendor = CameraVendor.GENERIC
    location: str | None = None
    confidence_min: int = 80
    confidence_review_min: int = 60
    max_clock_skew_seconds: int = 120
    heartbeat_interval_sec: int = 300


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateCameraCommand:
    name: str | None = None
    location: str | None = None
    confidence_min: int | None = None
    confidence_review_min: int | None = None
    max_clock_skew_seconds: int | None = None
    heartbeat_interval_sec: int | None = None
    active: bool | None = None
    video_endpoint: str | None = None  # запись на сервере: адрес, логин и пароль камеры — только все три вместе
    video_username: str | None = None
    video_password: str | None = None
    video_off: bool = False  # убрать адрес и пароль видео (камера больше не пишется сервером)


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateBarrierCommand:
    lot_id: UUID
    name: str
    driver: BarrierDriverKind
    endpoint: str
    camera_id: UUID | None = None
    auth_secret_ref: str | None = None
    auto_close_seconds: int | None = None
    username: str | None = None  # HIKVISION_ISAPI: логин камеры (пароль не возвращается API)
    password: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateBarrierCommand:
    name: str | None = None
    endpoint: str | None = None
    camera_id: UUID | None = None
    auth_secret_ref: str | None = None
    auto_close_seconds: int | None = None
    active: bool | None = None
    username: str | None = None  # новые логин и пароль камеры — меняются только парой
    password: str | None = None


class DeviceAdminService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], AdminRepositories],
        resolver: EndpointResolver,
        auth_cache: CameraAuthCache,
        pepper: bytes,
        clock: Clock,
        gate: SubscriptionGate | None = None,
        drivers: DriverRegistry | None = None,
        box: CredentialBox | None = None,
    ) -> None:
        self._drivers = drivers  # v2: проверка связи со шлагбаумом из «Устройств»
        self._box = box  # логины контроллеров (HIKVISION_ISAPI) шифруются ключом клиента
        self._uow_factory = uow_factory
        self._repos = repos
        self._resolver = resolver
        self._auth_cache = auth_cache
        self._pepper = pepper
        self._clock = clock
        self._gate = gate  # v2: лимит камер тарифа платформы

    async def active_camera_count(self, tenant_id: UUID) -> int:
        async with self._uow_factory(tenant_id) as uow:
            return sum(1 for c in await self._repos(uow).cameras.list(lot_ids=None) if c.active)

    # ---------- камеры ----------

    async def list_cameras(self, actor: CurrentUser) -> list[Camera]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).cameras.list(lot_ids=actor.allowed_lots())

    async def get_camera(self, actor: CurrentUser, camera_id: UUID) -> Camera:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._camera(uow, actor, camera_id)

    async def create_camera(self, actor: CurrentUser, cmd: CreateCameraCommand, *, reason: str) -> Camera:
        self._check_lot(actor, cmd.lot_id)
        camera = Camera(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            lot_id=cmd.lot_id,
            code=cmd.code,
            name=cmd.name,
            direction=cmd.direction,
            vendor=cmd.vendor,
            location=cmd.location,
            confidence_min=cmd.confidence_min,
            confidence_review_min=cmd.confidence_review_min,
            max_clock_skew_seconds=cmd.max_clock_skew_seconds,
            heartbeat_interval_sec=cmd.heartbeat_interval_sec,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(cmd.lot_id) is None:
                raise NotFoundError("lot not found")
            if any(c.code == camera.code for c in await repos.cameras.list(lot_ids=frozenset({cmd.lot_id}))):
                raise ConflictError("camera code already exists in this lot")
            if self._gate is not None:
                active = sum(1 for c in await repos.cameras.list(lot_ids=None) if c.active)
                await self._gate.ensure(actor.tenant_id, "cameras", active + 1)
            await repos.cameras.add(camera)
            await self._audit(repos, actor, "CAMERA_CREATED", "camera", camera.id, camera.lot_id, reason)
        return camera

    async def update_camera(
        self, actor: CurrentUser, camera_id: UUID, cmd: UpdateCameraCommand, *, reason: str
    ) -> Camera:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            camera = await self._camera(uow, actor, camera_id, for_update=True)
            camera.update(
                name=cmd.name,
                location=cmd.location,
                confidence_min=cmd.confidence_min,
                confidence_review_min=cmd.confidence_review_min,
                max_clock_skew_seconds=cmd.max_clock_skew_seconds,
                heartbeat_interval_sec=cmd.heartbeat_interval_sec,
                active=cmd.active,
            )
            await self._update_camera_video(repos, actor, camera, cmd)
            await repos.cameras.save(camera)
            await self._audit(repos, actor, "CAMERA_UPDATED", "camera", camera.id, camera.lot_id, reason)
            if cmd.active is False:
                for key in await repos.keys.list_for_camera(camera.id):
                    uow.after_commit(self._invalidator(key.key_prefix))
        return camera

    async def _update_camera_video(
        self, repos: AdminRepositories, actor: CurrentUser, camera: Camera, cmd: UpdateCameraCommand
    ) -> None:
        """Адрес и логин видеопотока: меняются только вместе (иначе роль с правом правки камер увела бы сохранённый
        пароль на свой адрес), адрес — по SSRF-политике площадки, как у стрел."""
        if cmd.video_off:
            camera.set_video(None, None)
            return
        given = (cmd.video_endpoint, cmd.video_username, cmd.video_password)
        if all(v is None for v in given):
            return
        endpoint, username, password = (v or "" for v in given)
        if not endpoint.strip() or not username.strip() or not password:
            raise ValidationError(
                "camera video address, login and password are all required", code="CREDENTIALS_REQUIRED"
            )
        lot = await repos.lots.get(camera.lot_id)
        if lot is None:
            raise NotFoundError("lot not found")
        await self.validate_endpoint(endpoint, BarrierDriverKind.HIKVISION_ISAPI, lot.policies.barrier_cidr_allowlist)
        sealed = self._seal(actor.tenant_id, f"camera-video:{camera.id}", username, password)
        camera.set_video(endpoint, sealed)

    # ---------- ключи ----------

    async def list_keys(self, actor: CurrentUser, camera_id: UUID) -> list[CameraApiKey]:
        async with self._uow_factory(actor.tenant_id) as uow:
            await self._camera(uow, actor, camera_id)
            return await self._repos(uow).keys.list_for_camera(camera_id)

    async def issue_key(
        self, actor: CurrentUser, camera_id: UUID, *, label: str | None, expires_in: timedelta | None, reason: str
    ) -> IssuedKey:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            camera = await self._camera(uow, actor, camera_id)
            issued = self._new_key(actor, camera, label=label, expires_in=expires_in)
            await repos.keys.add(issued.key)
            await self._audit(
                repos,
                actor,
                "CAMERA_KEY_ISSUED",
                "camera_api_key",
                issued.key.id,
                camera.lot_id,
                reason,
                payload={"camera_id": str(camera.id), "key_prefix": issued.key.key_prefix},
            )
        return issued

    async def rotate_key(
        self, actor: CurrentUser, key_id: UUID, *, grace: timedelta = DEFAULT_ROTATION_GRACE, reason: str
    ) -> IssuedKey:
        """Новый ключ + старый доживает окно перекрытия (A-17), после чего отклоняется по expires_at."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            old = await repos.keys.get(key_id, for_update=True)
            if old is None:
                raise NotFoundError("camera key not found")
            camera = await self._camera(uow, actor, old.camera_id)
            old.expire_after(now=self._clock.now(), grace=grace)
            await repos.keys.save(old)
            issued = self._new_key(actor, camera, label=old.label, expires_in=None)
            await repos.keys.add(issued.key)
            await self._audit(
                repos,
                actor,
                "CAMERA_KEY_ROTATED",
                "camera_api_key",
                old.id,
                camera.lot_id,
                reason,
                payload={"new_key_id": str(issued.key.id), "grace_seconds": int(grace.total_seconds())},
            )
            uow.after_commit(self._invalidator(old.key_prefix))
        return issued

    async def revoke_key(self, actor: CurrentUser, key_id: UUID, *, reason: str) -> CameraApiKey:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            key = await repos.keys.get(key_id, for_update=True)
            if key is None:
                raise NotFoundError("camera key not found")
            camera = await self._camera(uow, actor, key.camera_id)
            key.revoke(now=self._clock.now(), by=actor.user_id)
            await repos.keys.save(key)
            await self._audit(repos, actor, "CAMERA_KEY_REVOKED", "camera_api_key", key.id, camera.lot_id, reason)
            uow.publish(
                IntegrationEvent(
                    type="devices.camera_key.revoked.v1",
                    tenant_id=actor.tenant_id,
                    aggregate_type="camera_api_key",
                    aggregate_id=key.id,
                    payload={"key_id": str(key.id), "camera_id": str(key.camera_id), "key_prefix": key.key_prefix},
                )
            )
            uow.after_commit(self._invalidator(key.key_prefix))
        return key

    def _new_key(
        self, actor: CurrentUser, camera: Camera, *, label: str | None, expires_in: timedelta | None
    ) -> IssuedKey:
        raw = generate_raw_key()
        prefix = split_prefix(raw)
        assert prefix is not None
        now = self._clock.now()
        key = CameraApiKey(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            camera_id=camera.id,
            key_prefix=prefix,
            key_hmac=hmac_camera_key(self._pepper, raw),
            label=label,
            created_at=now,
            created_by=actor.user_id,
            expires_at=now + expires_in if expires_in else None,
        )
        return IssuedKey(key=key, raw=raw)

    def _invalidator(self, prefix: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            try:
                await self._auth_cache.invalidate(prefix)
            except Exception as exc:  # кеш истечёт сам через TTL; событие revoked — страховка
                log.warning("camera_auth_cache_invalidate_failed", error_type=type(exc).__name__)

        return run

    # ---------- шлагбаумы ----------

    async def list_barriers(self, actor: CurrentUser) -> list[Barrier]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).barriers.list(lot_ids=actor.allowed_lots())

    async def get_barrier(self, actor: CurrentUser, barrier_id: UUID) -> Barrier:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._barrier(uow, actor, barrier_id)

    def _seal_credentials(
        self, tenant_id: UUID, barrier_id: UUID, username: str | None, password: str | None
    ) -> bytes | None:
        if username is None and password is None:
            return None
        if not username or not username.strip() or not password:
            raise ValidationError("camera login and password are both required", code="CREDENTIALS_REQUIRED")
        return self._seal(tenant_id, f"barrier:{barrier_id}", username, password)

    def _seal(self, tenant_id: UUID, purpose: str, username: str, password: str) -> bytes:
        if self._box is None:
            raise ValidationError("controller credentials are not supported here")
        secret = json.dumps({"username": username.strip(), "password": password}).encode()
        return self._box.seal(tenant_id, purpose, secret)

    async def create_barrier(self, actor: CurrentUser, cmd: CreateBarrierCommand, *, reason: str) -> Barrier:
        self._check_lot(actor, cmd.lot_id)
        barrier_id = uuid4()
        barrier = Barrier(
            id=barrier_id,
            tenant_id=actor.tenant_id,
            lot_id=cmd.lot_id,
            name=cmd.name,
            driver=cmd.driver,
            endpoint=cmd.endpoint,
            camera_id=cmd.camera_id,
            auth_secret_ref=cmd.auth_secret_ref,
            auto_close_seconds=cmd.auto_close_seconds,
            credentials_encrypted=self._seal_credentials(actor.tenant_id, barrier_id, cmd.username, cmd.password),
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get(cmd.lot_id)
            if lot is None:
                raise NotFoundError("lot not found")
            await self.validate_endpoint(barrier.endpoint, barrier.driver, lot.policies.barrier_cidr_allowlist)
            await repos.barriers.add(barrier)
            await self._audit(repos, actor, "BARRIER_CREATED", "barrier", barrier.id, barrier.lot_id, reason)
        return barrier

    async def update_barrier(
        self, actor: CurrentUser, barrier_id: UUID, cmd: UpdateBarrierCommand, *, reason: str
    ) -> Barrier:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            barrier = await self._barrier(uow, actor, barrier_id, for_update=True)
            credentials = self._seal_credentials(actor.tenant_id, barrier.id, cmd.username, cmd.password)
            moved = cmd.endpoint is not None and cmd.endpoint.strip() != barrier.endpoint
            if moved and barrier.credentials_encrypted is not None and credentials is None:
                # логин и пароль камеры уходят на адрес стрелы (Digest): новый адрес — только вместе с ними, иначе
                # роль с правом правки стрел увела бы их на свой сервер «Проверить связь»
                raise ValidationError(
                    "camera login and password must be entered again when the address changes",
                    code="CREDENTIALS_REQUIRED",
                )
            barrier.update(
                name=cmd.name,
                endpoint=cmd.endpoint,
                camera_id=cmd.camera_id,
                auth_secret_ref=cmd.auth_secret_ref,
                auto_close_seconds=cmd.auto_close_seconds,
                active=cmd.active,
                credentials_encrypted=credentials,
            )
            if cmd.endpoint is not None:
                lot = await repos.lots.get(barrier.lot_id)
                assert lot is not None
                await self.validate_endpoint(barrier.endpoint, barrier.driver, lot.policies.barrier_cidr_allowlist)
            await repos.barriers.save(barrier)
            await self._audit(repos, actor, "BARRIER_UPDATED", "barrier", barrier.id, barrier.lot_id, reason)
        return barrier

    async def validate_endpoint(self, endpoint: str, driver: BarrierDriverKind, allowlist: tuple[str, ...]) -> None:
        """SSRF: разбор, резолв DNS, блок-лист (127/8, 169.254/16, ::1, fd00::/8, multicast…), приватные — только
        по аллоклисту площадки."""
        parsed = parse_endpoint(endpoint, driver=driver.value)
        addresses = await self._resolver.resolve(parsed.host)
        check_addresses(addresses, allowlist)

    async def open_barrier(self, actor: CurrentUser, barrier_id: UUID, *, reason: str) -> BarrierCommandResult:
        """Ручное открытие: команда MANUAL с issued_by и reason (I-15) — аудит пишет request_open."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            barrier = await self._barrier(uow, actor, barrier_id)
            if not barrier.active:
                raise ValidationError("barrier is inactive")
            commands = SqlBarrierCommands(uow, repos.devices, self._clock)
            key = hashlib.sha256(
                f"manual:{barrier.id}:{actor.user_id}:{self._clock.now().isoformat()}".encode()
            ).digest()
            return await commands.request_open(
                lot_id=barrier.lot_id,
                camera_id=barrier.camera_id,
                session_id=None,
                saga_id=None,
                source=CommandSource.MANUAL,
                idempotency_key=key,
                issued_by=actor.user_id,
                reason=reason,
            )

    async def command(self, actor: CurrentUser, command_id: UUID) -> BarrierCommand:
        """v2: итог команды шлагбаума (кнопка «Открыть для проверки» ждёт OK/FAILED, а не только постановку)."""
        async with self._uow_factory(actor.tenant_id) as uow:
            command = await self._repos(uow).devices.commands.get(command_id)
        allowed = actor.allowed_lots()
        if command is None or (allowed is not None and command.lot_id not in allowed):
            raise NotFoundError("barrier command not found")
        return command

    async def probe_barrier(self, actor: CurrentUser, barrier_id: UUID, *, reason: str) -> DriverResult:
        """Проверка связи с контроллером без движения стрелы. Исходящий вызов — вне транзакции (I-9), затем аудит."""
        async with self._uow_factory(actor.tenant_id) as uow:
            await self._barrier(uow, actor, barrier_id)  # скоуп площадок и RLS
            info = await self._repos(uow).barriers.get(barrier_id)
        if info is None:
            raise NotFoundError("barrier not found")
        if self._drivers is None:
            raise ServiceUnavailableError("barrier drivers are not configured")
        result = await self._drivers.driver_for(info).probe(info)
        async with self._uow_factory(actor.tenant_id) as uow:
            await self._audit(
                self._repos(uow),
                actor,
                "BARRIER_PROBED",
                "barrier",
                barrier_id,
                info.lot_id,
                reason,
                payload={"ok": result.ok, "latency_ms": result.latency_ms, "error": result.error},
            )
        return result

    # ---------- общее ----------

    @staticmethod
    def _check_lot(actor: CurrentUser, lot_id: UUID) -> None:
        allowed = actor.allowed_lots()
        if allowed is not None and lot_id not in allowed:
            raise NotFoundError("lot not found")

    async def _camera(
        self, uow: UnitOfWork, actor: CurrentUser, camera_id: UUID, *, for_update: bool = False
    ) -> Camera:
        camera = await self._repos(uow).cameras.get(camera_id, for_update=for_update)
        allowed = actor.allowed_lots()
        if camera is None or (allowed is not None and camera.lot_id not in allowed):
            raise NotFoundError("camera not found")
        return camera

    async def _barrier(
        self, uow: UnitOfWork, actor: CurrentUser, barrier_id: UUID, *, for_update: bool = False
    ) -> Barrier:
        barrier = await self._repos(uow).barriers.get_entity(barrier_id, for_update=for_update)
        allowed = actor.allowed_lots()
        if barrier is None or (allowed is not None and barrier.lot_id not in allowed):
            raise NotFoundError("barrier not found")
        return barrier

    @staticmethod
    async def _audit(
        repos: AdminRepositories,
        actor: CurrentUser,
        action: str,
        entity_type: str,
        entity_id: UUID,
        lot_id: UUID,
        reason: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                lot_id=lot_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload=dict(payload or {}),
            )
        )
