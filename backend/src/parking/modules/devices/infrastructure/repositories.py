from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.devices.application.ports import BarrierInfo, CameraHealthRow, KeyCandidate
from parking.modules.devices.domain.barrier import Barrier, BarrierDriverKind, BarrierState
from parking.modules.devices.domain.camera import Camera, CameraApiKey, CameraDirection, CameraStatus, CameraVendor
from parking.modules.devices.domain.command import BarrierCommand, CommandKind, CommandSource, CommandState
from parking.platform.db.uow import UnitOfWork
from parking.shared.ttl_cache import TtlCache

BarrierCache = TtlCache[tuple[UUID, UUID, UUID | None], tuple[BarrierInfo | None]]
"""Кеш процесса (tenant_id, lot_id, camera_id) → (шлагбаум | None): «нет шлагбаума» тоже кешируется на TTL."""

_CMD_COLUMNS = """
    id, tenant_id, lot_id, barrier_id, kind::text AS kind, state::text AS state, source::text AS source, idempotency_key, correlation_id,
    created_at, session_id, saga_id, issued_by, reason, attempts, last_error, latency_ms
"""


def _command(r: Any) -> BarrierCommand:
    return BarrierCommand(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        barrier_id=r.barrier_id,
        kind=CommandKind(r.kind),
        source=CommandSource(r.source),
        idempotency_key=bytes(r.idempotency_key),
        correlation_id=r.correlation_id,
        created_at=r.created_at,
        state=CommandState(r.state),
        session_id=r.session_id,
        saga_id=r.saga_id,
        issued_by=r.issued_by,
        reason=r.reason,
        attempts=r.attempts,
        last_error=r.last_error,
        latency_ms=r.latency_ms,
    )


_BARRIER_INFO = """
    SELECT b.id, b.lot_id, b.driver::text AS driver, b.endpoint, b.auth_secret_ref, b.active, b.camera_id, b.name,
           l.barrier_cidr_allowlist::text[] AS allowlist, b.tenant_id, b.credentials_encrypted
    FROM barrier b JOIN parking_lot l ON l.id = b.lot_id
"""
_BARRIER_COLUMNS = """
    id, tenant_id, lot_id, camera_id, name, driver::text AS driver, endpoint, auth_secret_ref, auto_close_seconds, active,
    state::text AS state, credentials_encrypted
"""
_CAMERA_COLUMNS = """
    id, tenant_id, lot_id, code, name, direction::text AS direction, vendor::text AS vendor, location, confidence_min,
    confidence_review_min, max_clock_skew_seconds, heartbeat_interval_sec, active, status::text AS status,
    last_heartbeat_at, last_event_at, firmware_version
"""
_KEY_COLUMNS = """
    id, tenant_id, camera_id, key_prefix, key_hmac, pepper_version, label, active, created_at, created_by, expires_at,
    last_used_at, revoked_at, revoked_by
"""


def _barrier(r: Any) -> BarrierInfo:
    return BarrierInfo(
        id=r.id,
        lot_id=r.lot_id,
        driver=r.driver,
        endpoint=r.endpoint,
        auth_secret_ref=r.auth_secret_ref,
        active=r.active,
        camera_id=r.camera_id,
        name=r.name,
        allowlist=tuple(r.allowlist or ()),
        tenant_id=r.tenant_id,
        credentials_encrypted=bytes(r.credentials_encrypted) if r.credentials_encrypted is not None else None,
    )


def _barrier_entity(r: Any) -> Barrier:
    return Barrier(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        camera_id=r.camera_id,
        name=r.name,
        driver=BarrierDriverKind(r.driver),
        endpoint=r.endpoint,
        auth_secret_ref=r.auth_secret_ref,
        auto_close_seconds=r.auto_close_seconds,
        active=r.active,
        state=BarrierState(r.state),
        credentials_encrypted=bytes(r.credentials_encrypted) if r.credentials_encrypted is not None else None,
    )


def _camera(r: Any) -> Camera:
    return Camera(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        code=r.code,
        name=r.name,
        direction=CameraDirection(r.direction),
        vendor=CameraVendor(r.vendor),
        location=r.location,
        confidence_min=r.confidence_min,
        confidence_review_min=r.confidence_review_min,
        max_clock_skew_seconds=r.max_clock_skew_seconds,
        heartbeat_interval_sec=r.heartbeat_interval_sec,
        active=r.active,
        status=CameraStatus(r.status),
        last_heartbeat_at=r.last_heartbeat_at,
        last_event_at=r.last_event_at,
        firmware_version=r.firmware_version,
    )


def _key(r: Any) -> CameraApiKey:
    return CameraApiKey(
        id=r.id,
        tenant_id=r.tenant_id,
        camera_id=r.camera_id,
        key_prefix=r.key_prefix,
        key_hmac=bytes(r.key_hmac),
        pepper_version=r.pepper_version,
        label=r.label,
        active=r.active,
        created_at=r.created_at,
        created_by=r.created_by,
        expires_at=r.expires_at,
        last_used_at=r.last_used_at,
        revoked_at=r.revoked_at,
        revoked_by=r.revoked_by,
    )


class SqlBarrierRepository:
    def __init__(
        self, session: AsyncSession, *, cache: BarrierCache | None = None, tenant_id: UUID | None = None
    ) -> None:
        self._s = session
        self._cache = cache if tenant_id is not None else None
        self._tenant_id = tenant_id

    async def for_camera(self, lot_id: UUID, camera_id: UUID | None) -> BarrierInfo | None:
        """Шлагбаум камеры (иначе первый активный на площадке); через кеш процесса на горячем пути ENTRY."""
        key = (self._tenant_id, lot_id, camera_id) if self._tenant_id is not None else None
        if self._cache is not None and key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached[0]
        barrier = await self._for_camera_sql(lot_id, camera_id)
        if self._cache is not None and key is not None:
            self._cache.set(key, (barrier,))
        return barrier

    async def _for_camera_sql(self, lot_id: UUID, camera_id: UUID | None) -> BarrierInfo | None:
        row = None
        if camera_id is not None:
            row = (
                await self._s.execute(
                    text(f"{_BARRIER_INFO} WHERE b.lot_id = :lot AND b.camera_id = :cam AND b.active"),
                    {"lot": lot_id, "cam": camera_id},
                )
            ).first()
        if row is None:
            row = (
                await self._s.execute(
                    text(f"{_BARRIER_INFO} WHERE b.lot_id = :lot AND b.active ORDER BY b.created_at LIMIT 1"),
                    {"lot": lot_id},
                )
            ).first()
        return None if row is None else _barrier(row)

    async def get(self, barrier_id: UUID) -> BarrierInfo | None:
        row = (await self._s.execute(text(f"{_BARRIER_INFO} WHERE b.id = :id"), {"id": barrier_id})).first()
        return None if row is None else _barrier(row)

    async def get_entity(self, barrier_id: UUID, *, for_update: bool = False) -> Barrier | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_BARRIER_COLUMNS} FROM barrier WHERE id = :id{lock}"), {"id": barrier_id}
            )
        ).first()
        return None if row is None else _barrier_entity(row)

    async def list(self, *, lot_ids: frozenset[UUID] | None) -> list[Barrier]:
        where = "" if lot_ids is None else " WHERE lot_id = ANY(:lots)"
        rows = await self._s.execute(
            text(f"SELECT {_BARRIER_COLUMNS} FROM barrier{where} ORDER BY lot_id, name"),
            {} if lot_ids is None else {"lots": list(lot_ids)},
        )
        return [_barrier_entity(r) for r in rows]

    async def add(self, b: Barrier) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO barrier (id, tenant_id, lot_id, camera_id, name, driver, endpoint, auth_secret_ref, auto_close_seconds, active,
                                     credentials_encrypted)
                VALUES (:id, :tenant, :lot, :camera, :name, CAST(:driver AS barrier_driver), :endpoint, :secret, :auto_close, :active,
                        :creds)
                """
            ),
            {
                "id": b.id,
                "tenant": b.tenant_id,
                "lot": b.lot_id,
                "camera": b.camera_id,
                "name": b.name,
                "driver": b.driver.value,
                "endpoint": b.endpoint,
                "secret": b.auth_secret_ref,
                "auto_close": b.auto_close_seconds,
                "active": b.active,
                "creds": b.credentials_encrypted,
            },
        )
        self._invalidate(b.lot_id)

    async def save(self, b: Barrier) -> None:
        await self._s.execute(
            text(
                """
                UPDATE barrier SET camera_id = :camera, name = :name, endpoint = :endpoint, auth_secret_ref = :secret,
                    auto_close_seconds = :auto_close, active = :active, credentials_encrypted = :creds
                WHERE id = :id
                """
            ),
            {
                "id": b.id,
                "camera": b.camera_id,
                "name": b.name,
                "endpoint": b.endpoint,
                "secret": b.auth_secret_ref,
                "auto_close": b.auto_close_seconds,
                "active": b.active,
                "creds": b.credentials_encrypted,
            },
        )
        self._invalidate(b.lot_id)

    def _invalidate(self, lot_id: UUID) -> None:
        if self._cache is not None:
            self._cache.invalidate_where(lambda k: k[1] == lot_id)


class SqlCameraRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, camera_id: UUID, *, for_update: bool = False) -> Camera | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(text(f"SELECT {_CAMERA_COLUMNS} FROM camera WHERE id = :id{lock}"), {"id": camera_id})
        ).first()
        return None if row is None else _camera(row)

    async def list(self, *, lot_ids: frozenset[UUID] | None) -> list[Camera]:
        where = "" if lot_ids is None else " WHERE lot_id = ANY(:lots)"
        rows = await self._s.execute(
            text(f"SELECT {_CAMERA_COLUMNS} FROM camera{where} ORDER BY lot_id, code"),
            {} if lot_ids is None else {"lots": list(lot_ids)},
        )
        return [_camera(r) for r in rows]

    async def add(self, c: Camera) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO camera (id, tenant_id, lot_id, code, name, direction, vendor, location, confidence_min, confidence_review_min,
                                    max_clock_skew_seconds, heartbeat_interval_sec, active)
                VALUES (:id, :tenant, :lot, :code, :name, CAST(:direction AS camera_direction), CAST(:vendor AS camera_vendor), :location,
                        :cmin, :crmin, :skew, :hb, :active)
                """
            ),
            {
                "id": c.id,
                "tenant": c.tenant_id,
                "lot": c.lot_id,
                "code": c.code,
                "name": c.name,
                "direction": c.direction.value,
                "vendor": c.vendor.value,
                "location": c.location,
                "cmin": c.confidence_min,
                "crmin": c.confidence_review_min,
                "skew": c.max_clock_skew_seconds,
                "hb": c.heartbeat_interval_sec,
                "active": c.active,
            },
        )

    async def save(self, c: Camera) -> None:
        await self._s.execute(
            text(
                """
                UPDATE camera SET name = :name, location = :location, confidence_min = :cmin, confidence_review_min = :crmin,
                    max_clock_skew_seconds = :skew, heartbeat_interval_sec = :hb, active = :active
                WHERE id = :id
                """
            ),
            {
                "id": c.id,
                "name": c.name,
                "location": c.location,
                "cmin": c.confidence_min,
                "crmin": c.confidence_review_min,
                "skew": c.max_clock_skew_seconds,
                "hb": c.heartbeat_interval_sec,
                "active": c.active,
            },
        )


class SqlCameraKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, k: CameraApiKey) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO camera_api_key (id, tenant_id, camera_id, key_prefix, key_hmac, pepper_version, label, created_by, expires_at)
                VALUES (:id, :tenant, :camera, :prefix, :hmac, :pv, :label, :created_by, :expires)
                """
            ),
            {
                "id": k.id,
                "tenant": k.tenant_id,
                "camera": k.camera_id,
                "prefix": k.key_prefix,
                "hmac": k.key_hmac,
                "pv": k.pepper_version,
                "label": k.label,
                "created_by": k.created_by,
                "expires": k.expires_at,
            },
        )

    async def get(self, key_id: UUID, *, for_update: bool = False) -> CameraApiKey | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_KEY_COLUMNS} FROM camera_api_key WHERE id = :id{lock}"), {"id": key_id}
            )
        ).first()
        return None if row is None else _key(row)

    async def list_for_camera(self, camera_id: UUID) -> list[CameraApiKey]:
        rows = await self._s.execute(
            text(f"SELECT {_KEY_COLUMNS} FROM camera_api_key WHERE camera_id = :cam ORDER BY created_at DESC"),
            {"cam": camera_id},
        )
        return [_key(r) for r in rows]

    async def save(self, k: CameraApiKey) -> None:
        await self._s.execute(
            text(
                "UPDATE camera_api_key SET active = :active, expires_at = :expires, revoked_at = :revoked_at, revoked_by = :revoked_by WHERE id = :id"
            ),
            {
                "id": k.id,
                "active": k.active,
                "expires": k.expires_at,
                "revoked_at": k.revoked_at,
                "revoked_by": k.revoked_by,
            },
        )


class SqlCameraKeyLookup:
    """Pre-tenant кандидаты по префиксу: SECURITY DEFINER app.lookup_camera_keys (UoW без tenant)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def candidates(self, key_prefix: str) -> list[KeyCandidate]:
        rows = await self._s.execute(text("SELECT * FROM app.lookup_camera_keys(:prefix)"), {"prefix": key_prefix})
        return [
            KeyCandidate(
                key_id=r.key_id,
                tenant_id=r.tenant_id,
                camera_id=r.camera_id,
                lot_id=r.lot_id,
                key_hmac=bytes(r.key_hmac),
                pepper_version=r.pepper_version,
                expires_at=r.expires_at,
                camera_active=r.camera_active,
                direction=r.direction,
                code=r.code,
                confidence_min=r.confidence_min,
                confidence_review_min=r.confidence_review_min,
                max_clock_skew_seconds=r.max_clock_skew_seconds,
                late_event_window_hours=r.late_event_window_hours,
                tenant_active=r.tenant_active,
            )
            for r in rows
        ]


class SqlCameraHealthStore:
    """Кросс-tenant чтение активных камер ролью app_maintenance; запись статусов — в tenant-UoW (см. CameraHealthJob)."""

    def __init__(self, maintenance_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = maintenance_factory

    async def active_cameras(self) -> list[CameraHealthRow]:
        async with self._factory() as session, session.begin():
            rows = await session.execute(
                text(
                    "SELECT id, tenant_id, lot_id, heartbeat_interval_sec, status::text AS status, last_heartbeat_at, last_event_at FROM camera WHERE active"
                )
            )
            return [
                CameraHealthRow(
                    camera_id=r.id,
                    tenant_id=r.tenant_id,
                    lot_id=r.lot_id,
                    heartbeat_interval_sec=r.heartbeat_interval_sec,
                    status=CameraStatus(r.status),
                    last_heartbeat_at=r.last_heartbeat_at,
                    last_event_at=r.last_event_at,
                )
                for r in rows
            ]


class SqlCameraHealthWriter:
    """Батч-флаш ролью app_bulk (GRANT UPDATE на отдельные колонки): один executemany на таблицу, монотонно."""

    async def flush(
        self,
        uow: UnitOfWork,
        *,
        heartbeats: Sequence[tuple[UUID, datetime]],
        events: Sequence[tuple[UUID, datetime]],
        key_usage: Sequence[tuple[UUID, datetime]],
    ) -> None:
        session = uow.session
        if heartbeats:
            await session.execute(
                text(
                    "UPDATE camera SET last_heartbeat_at = GREATEST(coalesce(last_heartbeat_at, :at), :at) WHERE id = :id"
                ),
                [{"id": cid, "at": at} for cid, at in heartbeats],
            )
        if events:
            await session.execute(
                text("UPDATE camera SET last_event_at = GREATEST(coalesce(last_event_at, :at), :at) WHERE id = :id"),
                [{"id": cid, "at": at} for cid, at in events],
            )
        if key_usage:
            await session.execute(
                text(
                    "UPDATE camera_api_key SET last_used_at = GREATEST(coalesce(last_used_at, :at), :at) WHERE id = :id"
                ),
                [{"id": kid, "at": at} for kid, at in key_usage],
            )

    async def update_status(self, uow: UnitOfWork, camera_id: UUID, status: CameraStatus) -> None:
        await uow.session.execute(
            text("UPDATE camera SET status = CAST(:status AS camera_status) WHERE id = :id"),
            {"id": camera_id, "status": status.value},
        )


class SqlBarrierCommandRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, c: BarrierCommand) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO barrier_command (id, created_at, tenant_id, lot_id, barrier_id, kind, state, source, idempotency_key, correlation_id,
                                             session_id, saga_id, issued_by, reason, attempts)
                VALUES (:id, :created_at, :tenant, :lot, :barrier, CAST(:kind AS barrier_command_kind), CAST(:state AS barrier_command_state),
                        CAST(:source AS barrier_command_source), :key, :correlation, :session, :saga, :issued_by, :reason, :attempts)
                """
            ),
            {
                "id": c.id,
                "created_at": c.created_at,
                "tenant": c.tenant_id,
                "lot": c.lot_id,
                "barrier": c.barrier_id,
                "kind": c.kind.value,
                "state": c.state.value,
                "source": c.source.value,
                "key": c.idempotency_key,
                "correlation": c.correlation_id,
                "session": c.session_id,
                "saga": c.saga_id,
                "issued_by": c.issued_by,
                "reason": c.reason,
                "attempts": c.attempts,
            },
        )

    async def find_by_key(self, idempotency_key: bytes) -> BarrierCommand | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_CMD_COLUMNS} FROM barrier_command WHERE idempotency_key = :key ORDER BY created_at DESC LIMIT 1"
                ),
                {"key": idempotency_key},
            )
        ).first()
        return None if row is None else _command(row)

    async def get(self, command_id: UUID) -> BarrierCommand | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_CMD_COLUMNS} FROM barrier_command WHERE id = :id"), {"id": command_id}
            )
        ).first()
        return None if row is None else _command(row)

    async def get_for_update(self, command_id: UUID) -> BarrierCommand | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_CMD_COLUMNS} FROM barrier_command WHERE id = :id FOR UPDATE"), {"id": command_id}
            )
        ).first()
        return None if row is None else _command(row)

    async def save(self, c: BarrierCommand) -> None:
        await self._s.execute(
            text(
                """
                UPDATE barrier_command SET state = CAST(:state AS barrier_command_state), attempts = :attempts, last_error = :error,
                    latency_ms = :latency, sent_at = CASE WHEN :state = 'SENT' THEN now() ELSE sent_at END,
                    completed_at = CASE WHEN :state IN ('OK', 'FAILED', 'SKIPPED') THEN now() ELSE completed_at END
                WHERE id = :id
                """
            ),
            {
                "id": c.id,
                "state": c.state.value,
                "attempts": c.attempts,
                "error": c.last_error,
                "latency": c.latency_ms,
            },
        )
