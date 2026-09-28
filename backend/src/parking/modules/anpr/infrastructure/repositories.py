from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SaTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.anpr.application.ports import IdempotencyRecord, StoredEvent


def is_connectivity_error(exc: BaseException) -> bool:
    """Ошибки соединения/пула — сигнал breaker'у БД; ошибки данных (constraint, syntax) — нет: их нельзя «переждать»."""
    if isinstance(exc, (ConnectionError, OSError, TimeoutError, SaTimeoutError, InterfaceError)):
        return True
    if isinstance(exc, asyncpg.PostgresConnectionError | asyncpg.InterfaceError):
        return True
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and is_connectivity_error(cause)


def is_timeout_error(exc: BaseException) -> bool:
    """Таймаут клиента/пула (сетевой разрыв без RST): один такой отказ = полный бюджет ожидания."""
    if isinstance(exc, (TimeoutError, SaTimeoutError)):
        return True
    cause = exc.__cause__ or exc.__context__
    return cause is not None and cause is not exc and is_timeout_error(cause)


class SqlAnprEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, st: StoredEvent) -> None:
        e = st.event
        await self._s.execute(
            text(
                """
                INSERT INTO anpr_event (id, event_time, tenant_id, lot_id, camera_id, event_type, direction, received_at, plate_hash,
                                        plate_encrypted, hmac_key_version, enc_key_version, confidence, image_object_key, recognition,
                                        clock_skew_seconds, late, queued_offline, result, reject_reason, session_id, saga_id,
                                        idempotency_key, correlation_id)
                VALUES (:id, :event_time, :tenant, :lot, :camera, CAST(:event_type AS anpr_event_type), CAST(:direction AS camera_direction),
                        :received_at, :plate_hash, :plate_encrypted, :hmac_v, :enc_v, :confidence, :image_key, CAST(:recognition AS jsonb),
                        :skew, :late, :queued, CAST(:result AS anpr_result), :reject_reason, :session, :saga, :key, :correlation)
                """
            ),
            {
                "id": e.event_id,
                "event_time": e.event_time,
                "tenant": e.tenant_id,
                "lot": e.lot_id,
                "camera": e.camera_id,
                "event_type": e.event_type.value,
                "direction": e.direction,
                "received_at": e.received_at,
                "plate_hash": st.plate_hash,
                "plate_encrypted": st.plate_encrypted,
                "hmac_v": st.hmac_key_version,
                "enc_v": st.enc_key_version,
                "confidence": e.confidence,
                "image_key": e.image_object_key,
                "recognition": json.dumps(e.recognition, default=str),
                "skew": e.time.skew_seconds,
                "late": st.late,
                "queued": e.queued_offline,
                "result": st.result,
                "reject_reason": st.reject_reason,
                "session": st.session_id,
                "saga": st.saga_id,
                "key": st.idempotency_key,
                "correlation": _uuid_or_new(e.correlation_id),
            },
        )

    async def images_for_session(self, session_id: UUID) -> list[tuple[UUID, str, datetime]]:
        rows = await self._s.execute(
            text(
                "SELECT id, image_object_key, event_time FROM anpr_event WHERE session_id = :sid AND image_object_key IS NOT NULL ORDER BY event_time"
            ),
            {"sid": session_id},
        )
        return [(r.id, r.image_object_key, r.event_time) for r in rows]


class SqlIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def lock(self, key: bytes) -> None:
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(encode(:key, 'hex'), 0))"), {"key": key}
        )

    async def find(self, key: bytes, event_second: datetime) -> IdempotencyRecord | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT event_id, result::text AS result, response FROM anpr_idempotency WHERE event_second = :sec AND idempotency_key = :key"
                ),
                {"sec": event_second, "key": key},
            )
        ).first()
        if row is None:
            return None
        response: dict[str, Any] = row.response if isinstance(row.response, dict) else json.loads(row.response)
        return IdempotencyRecord(event_id=row.event_id, result=row.result, response=response)

    async def add(
        self,
        key: bytes,
        event_second: datetime,
        *,
        tenant_id: UUID,
        camera_id: UUID,
        event_id: UUID | None,
        result: str,
        response: dict[str, Any],
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO anpr_idempotency (idempotency_key, event_second, tenant_id, camera_id, event_id, result, response)
                VALUES (:key, :sec, :tenant, :camera, :event, CAST(:result AS anpr_result), CAST(:response AS jsonb))
                """
            ),
            {
                "key": key,
                "sec": event_second,
                "tenant": tenant_id,
                "camera": camera_id,
                "event": event_id,
                "result": result,
                "response": json.dumps(response),
            },
        )


def _uuid_or_new(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        from uuid import uuid4  # noqa: PLC0415

        return uuid4()
