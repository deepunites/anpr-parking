"""Stream Consumer: consumer group на каждом шарде, аренда шарда (один активный потребитель → порядок на агрегат),
XAUTOCLAIM зависших pending, inbox-дедупликация, ретраи с бэкоффом (отложенная очередь, не блокирует шарды), DLQ (ADR-07)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from opentelemetry import context as otel_context
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import text

from parking.platform import metrics
from parking.platform.db.engine import Database, Pool
from parking.platform.logging import get_logger
from parking.platform.tracing import FLOW_ATTR, context_from_traceparent, span
from parking.shared.correlation import bind_log_context, correlation_scope
from parking.workers.envelope import Envelope, stream_name
from parking.workers.handlers import Handler

log = get_logger("consumer")
DLQ_STREAM = "events:dlq"
RETRY_BACKOFF_SECONDS = (1, 4, 16)  # задержка перед повтором по номеру попытки; дальше — 16 с


def delayed_key(stream: str) -> str:
    """ZSET отложенных повторов шарда: member — поля конверта, score — момент, когда запись вернуть в стрим."""
    return f"{stream}:delayed"


def retry_delay(attempt: int) -> float:
    return float(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1])


_INBOX_CHECK = text(
    "SELECT 1 FROM consumer_inbox WHERE event_created_at = :at AND consumer_group = :grp AND event_id = :id"
)
_INBOX_INSERT = text(
    "INSERT INTO consumer_inbox (event_created_at, consumer_group, event_id) VALUES (:at, :grp, :id) ON CONFLICT DO NOTHING"
)


class StreamConsumer:
    def __init__(
        self,
        database: Database,
        redis: Redis,
        handlers: Mapping[str, Handler],
        *,
        group: str = "core",
        consumer_name: str = "consumer-1",
        shards: int = 8,
        block_ms: int = 1000,
        batch: int = 100,
        max_attempts: int = 3,
        claim_idle_ms: int = 30_000,
        lease_ms: int = 15_000,
    ) -> None:
        self._db = database
        self._redis = redis
        self._handlers = handlers
        self._group = group
        self._name = consumer_name
        self._shards = shards
        self._block_ms = block_ms
        self._batch = batch
        self._max_attempts = max_attempts
        self._claim_idle_ms = claim_idle_ms
        self._lease_ms = lease_ms

    async def ensure_groups(self) -> None:
        for shard in range(self._shards):
            with contextlib.suppress(ResponseError):  # BUSYGROUP — группа уже есть
                await self._redis.xgroup_create(stream_name(shard), self._group, id="0", mkstream=True)

    async def _owned_shards(self) -> list[int]:
        owned = []
        for shard in range(self._shards):
            key = f"lease:{self._group}:{stream_name(shard)}"
            if await self._redis.set(key, self._name, nx=True, px=self._lease_ms):
                owned.append(shard)
            elif (await self._redis.get(key)) == self._name.encode():
                await self._redis.pexpire(key, self._lease_ms)
                owned.append(shard)
        return owned

    async def run_forever(self, stop: asyncio.Event) -> None:
        await self.ensure_groups()
        while not stop.is_set():
            processed = await self.poll_once()
            if processed == 0:
                await asyncio.sleep(0.05)

    async def poll_once(self, *, block_ms: int | None = None) -> int:
        """Один проход по своим шардам. Порядок гарантируется внутри шарда (агрегат → шард), поэтому шарды
        обрабатываются параллельно: последовательный обход давал задержку ≈10–20 с на каждый шаг саги под нагрузкой
        (фаза 8 §6). Возвращает число обработанных записей."""
        owned = await self._owned_shards()
        counts = await asyncio.gather(*(self._poll_shard(stream_name(shard), block_ms) for shard in owned))
        return sum(counts)

    async def _poll_shard(self, stream: str, block_ms: int | None) -> int:
        """Один шард: вернуть созревшие повторы, XAUTOCLAIM зависших, затем новые записи — строго по порядку."""
        processed = 0
        await self._release_due_retries(stream)
        _, claimed, _ = await self._redis.xautoclaim(
            stream, self._group, self._name, min_idle_time=self._claim_idle_ms, count=self._batch
        )
        for entry_id, fields in claimed:
            processed += await self._handle(stream, entry_id, fields)
        response: Any = await self._redis.xreadgroup(
            self._group, self._name, {stream: ">"}, count=self._batch, block=block_ms
        )  # block=None — без BLOCK: BLOCK 0 висел бы вечно и упирался в socket_timeout клиента
        for _stream, entries in response or []:
            for entry_id, fields in entries:
                processed += await self._handle(stream, entry_id, fields)
        with contextlib.suppress(ResponseError):
            metrics.redis_stream_lag.labels(stream=stream).set(await self._lag(stream))
        return processed

    async def _lag(self, stream: str) -> int:
        """Отставание группы: ещё не доставленные записи (XINFO GROUPS lag, Redis ≥ 7) + доставленные без ACK."""
        for group in await self._redis.xinfo_groups(stream):
            if group.get("name") == self._group.encode():
                return int(group.get("lag") or 0) + int(group.get("pending") or 0)
        return 0

    async def _handle(self, stream: str, entry_id: bytes, fields: dict[bytes, bytes]) -> int:
        envelope = Envelope.from_fields(fields)
        with correlation_scope(envelope.correlation_id, tenant_id=envelope.tenant_id):
            bind_log_context(
                lot_id=envelope.payload.get("lot_id"),
                session_id=envelope.payload.get("session_id"),
                camera_id=envelope.payload.get("camera_id"),
                event_type=envelope.event_type,
            )
            return await self._handle_in_context(stream, entry_id, envelope)

    async def _handle_in_context(self, stream: str, entry_id: bytes, envelope: Envelope) -> int:
        handler = self._handlers.get(envelope.event_type)
        started = time.perf_counter()
        # traceparent из конверта: спан консьюмера — продолжение трассы, начатой в API (фаза 7 §3)
        parent = context_from_traceparent(envelope.trace_parent)
        token = otel_context.attach(parent) if parent is not None else None
        try:
            with span(
                f"consume {envelope.event_type}",
                **{
                    FLOW_ATTR: "exit" if _is_exit_flow(envelope) else "other",
                    "tenant_id": envelope.tenant_id,
                    "lot_id": envelope.payload.get("lot_id"),
                    "session_id": envelope.payload.get("session_id"),
                    "event_type": envelope.event_type,
                },
            ):
                await self._run_handler(stream, entry_id, envelope, handler)
        finally:
            if token is not None:
                otel_context.detach(token)
        log.debug(
            "consumer_handled",
            event_type=envelope.event_type,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return 1

    async def _run_handler(self, stream: str, entry_id: bytes, envelope: Envelope, handler: Handler | None) -> None:
        try:
            if handler is not None and not await self._already_processed(envelope.occurred_at, envelope.event_id):
                await handler(envelope)
                await self._record_inbox(envelope.occurred_at, envelope.event_id)
            metrics.consumer_processed_total.labels(event_type=envelope.event_type, result="ok").inc()
        except Exception as exc:
            attempt = envelope.attempt + 1
            log.warning(
                "consumer_handler_failed",
                event_type=envelope.event_type,
                event_id=str(envelope.event_id),
                attempt=attempt,
                error_type=type(exc).__name__,
            )
            metrics.consumer_processed_total.labels(event_type=envelope.event_type, result="error").inc()
            if attempt >= self._max_attempts:
                await self._to_dlq(stream, envelope, attempt, f"{type(exc).__name__}: {exc}")
            else:
                await self._requeue(stream, envelope, attempt)
        await self._redis.xack(stream, self._group, entry_id)

    async def _requeue(self, stream: str, envelope: Envelope, attempt: int) -> None:
        """Повтор с бэкоффом 1/4/16 с: запись уходит в отложенную очередь шарда и возвращается в хвост стрима,
        когда срок наступит (`_release_due_retries`). Сон в цикле недопустим: один падающий обработчик останавливал бы
        все шарды процесса (нагрузочный прогон фазы 8: команды шлагбаума не успевали до дедлайна шага саги)."""
        fields = {str(k): str(v) for k, v in replace(envelope, attempt=attempt).to_fields().items()}
        due = time.time() + retry_delay(attempt)
        await self._redis.zadd(delayed_key(stream), {json.dumps(fields, sort_keys=True): due})
        metrics.consumer_retry_scheduled_total.labels(stream=stream).inc()

    async def _release_due_retries(self, stream: str) -> None:
        """Возвращает в стрим записи, чей срок повтора наступил. XADD раньше ZREM: при падении между ними запись
        попадёт в стрим дважды — второй экземпляр отсечёт inbox-дедупликация (at-least-once)."""
        key = delayed_key(stream)
        due: Any = await self._redis.zrangebyscore(key, "-inf", time.time(), start=0, num=self._batch)
        for member in due:
            fields: dict[Any, Any] = json.loads(member)
            await self._redis.xadd(stream, fields)
            await self._redis.zrem(key, member)

    async def _to_dlq(self, stream: str, envelope: Envelope, attempt: int, error: str) -> None:
        fields: dict[Any, Any] = {
            **envelope.to_fields(),
            "attempt": str(attempt),
            "error": error[:500],
            "source_stream": stream,
        }
        await self._redis.xadd(DLQ_STREAM, fields)
        metrics.consumer_dlq_total.labels(stream=stream).inc()
        log.error("consumer_dlq", event_type=envelope.event_type, event_id=str(envelope.event_id), error=error[:200])

    async def _already_processed(self, created_at: datetime, event_id: object) -> bool:
        async with self._db.session_factory(Pool.CRITICAL)() as session, session.begin():
            row = (await session.execute(_INBOX_CHECK, {"at": created_at, "grp": self._group, "id": event_id})).first()
            return row is not None

    async def _record_inbox(self, created_at: datetime, event_id: object) -> None:
        async with self._db.session_factory(Pool.CRITICAL)() as session, session.begin():
            await session.execute(_INBOX_INSERT, {"at": created_at, "grp": self._group, "id": event_id})


_EXIT_FLOW_EVENTS = ("parking.exit.", "billing.payment.", "devices.barrier.", "parking.session.closed")


def _is_exit_flow(envelope: Envelope) -> bool:
    return envelope.event_type.startswith(_EXIT_FLOW_EVENTS) or bool(envelope.payload.get("saga_id"))
