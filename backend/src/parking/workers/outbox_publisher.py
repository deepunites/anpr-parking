"""Outbox publisher (ADR-07): единственный активный процесс (advisory lock), NOTIFY + опрос, SKIP LOCKED, порядок по id,
XADD в шард по aggregate_id, at-least-once, экспоненциальный бэкофф, FAILED после M попыток, метрики."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.platform import metrics
from parking.platform.db.engine import Database, Pool
from parking.platform.events_registry import EventSchemaError, UnknownEventTypeError, validate_event
from parking.platform.logging import get_logger
from parking.workers.envelope import Envelope, shard_for, stream_name

log = get_logger("outbox.publisher")
LEADER_LOCK_ID = 7_431_001
DEFAULT_SHARDS = 8

_SELECT = text(
    """
    SELECT id, created_at, event_id, tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id, trace_parent, retry_count
    FROM outbox
    WHERE status = 'PENDING' AND (next_retry_at IS NULL OR next_retry_at <= now())
    ORDER BY id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
    """
)
_MARK_PUBLISHED = text(
    "UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE created_at = :created_at AND id = :id"
)
_MARK_RETRY = text(
    "UPDATE outbox SET retry_count = :retry, next_retry_at = :next, last_error = :error, status = CAST(:status AS outbox_status) "
    "WHERE created_at = :created_at AND id = :id"
)
_STATS = text(
    "SELECT count(*) AS pending, coalesce(extract(epoch FROM now() - min(created_at)), 0) AS lag FROM outbox WHERE status = 'PENDING'"
)


class OutboxPublisher:
    def __init__(
        self,
        database: Database,
        redis: Redis,
        *,
        maintenance_dsn: str,
        batch_size: int = 500,
        poll_interval: float = 0.25,
        shards: int = DEFAULT_SHARDS,
        maxlen: int = 1_000_000,
        max_attempts: int = 10,
    ) -> None:
        self._db = database
        self._redis = redis
        self._dsn = maintenance_dsn
        self._batch = batch_size
        self._poll = poll_interval
        self._shards = shards
        self._maxlen = maxlen
        self._max_attempts = max_attempts
        self._listener: asyncpg.Connection | None = None
        self._wakeup = asyncio.Event()
        self.is_leader = False

    # --- leadership -------------------------------------------------------
    async def acquire_leadership(self) -> bool:
        """Advisory lock на выделенном соединении (не через PgBouncer): жив, пока живо соединение."""
        if self._listener is None:
            self._listener = await asyncpg.connect(self._dsn)
            await self._listener.add_listener("outbox_new", self._on_notify)
        self.is_leader = bool(await self._listener.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_LOCK_ID))
        return self.is_leader

    async def release(self) -> None:
        if self._listener is not None:
            with contextlib.suppress(Exception):
                await self._listener.execute("SELECT pg_advisory_unlock($1)", LEADER_LOCK_ID)
            with contextlib.suppress(Exception):
                await self._listener.close()
            self._listener = None
        self.is_leader = False

    def _on_notify(self, *_: object) -> None:
        self._wakeup.set()

    # --- main loop ------------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                if not self.is_leader and not await self.acquire_leadership():
                    await asyncio.sleep(2.0)
                    continue
                published = await self.publish_batch()
                if published >= self._batch:
                    continue  # ещё есть работа
                self._wakeup.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll)
        finally:
            await self.release()

    async def publish_batch(self) -> int:
        started = time.perf_counter()
        published = 0
        async with self._db.session_factory(Pool.MAINTENANCE)() as session, session.begin():
            rows = (await session.execute(_SELECT, {"limit": self._batch})).all()
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
                envelope = Envelope(
                    event_id=row.event_id,
                    event_type=row.event_type,
                    tenant_id=row.tenant_id,
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    occurred_at=row.created_at,
                    correlation_id=str(row.correlation_id),
                    trace_parent=row.trace_parent,
                    payload=payload,
                    attempt=int(row.retry_count),
                )
                try:
                    validate_event(envelope.event_type, envelope.payload)
                    stream = stream_name(shard_for(envelope.aggregate_id, self._shards))
                    fields: dict[Any, Any] = dict(envelope.to_fields())
                    await self._redis.xadd(stream, fields, maxlen=self._maxlen, approximate=True)
                except (EventSchemaError, UnknownEventTypeError) as exc:
                    await self._fail(session, row, str(exc), permanent=True)
                    continue
                except Exception as exc:
                    await self._fail(session, row, f"{type(exc).__name__}: {exc}", permanent=False)
                    continue
                await session.execute(_MARK_PUBLISHED, {"created_at": row.created_at, "id": row.id})
                metrics.outbox_published_total.labels(event_type=envelope.event_type).inc()
                published += 1
                log.debug(
                    "outbox_event_published",
                    correlation_id=envelope.correlation_id,
                    tenant_id=str(envelope.tenant_id),
                    event_type=envelope.event_type,
                    event_id=str(envelope.event_id),
                    stream=stream,
                    trace_parent=envelope.trace_parent,
                )
            stats = (await session.execute(_STATS)).one()
            metrics.outbox_pending_total.set(int(stats.pending))
            metrics.outbox_publish_lag_seconds.set(float(stats.lag))
        metrics.outbox_batch_seconds.observe(time.perf_counter() - started)
        if published:
            log.info("outbox_batch_published", count=published)
        return published

    async def _fail(self, session: AsyncSession, row: Any, error: str, *, permanent: bool) -> None:
        retry = int(getattr(row, "retry_count", 0)) + 1
        failed = permanent or retry >= self._max_attempts
        fields = {
            "event_type": getattr(row, "event_type", None),
            "event_id": str(getattr(row, "event_id", "")),
            "correlation_id": str(getattr(row, "correlation_id", "")),
            "error": error[:200],
            "retry": retry,
        }
        if failed:
            metrics.outbox_failed_total.inc()
            log.error("outbox_event_failed", **fields)
        else:
            log.warning("outbox_publish_retry", **fields)
        await session.execute(
            _MARK_RETRY,
            {
                "retry": retry,
                "next": datetime.now(tz=UTC) + timedelta(seconds=min(2**retry, 300)),
                "error": error[:2000],
                "status": "FAILED" if failed else "PENDING",
                "created_at": row.created_at,
                "id": row.id,
            },
        )
