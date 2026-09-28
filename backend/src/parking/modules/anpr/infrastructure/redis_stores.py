"""Redis-части ingest: ускоритель ответов, скользящее окно rate limit, оффлайн-буфер (stream + группа), DLQ."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from typing import Any, ClassVar
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from parking.modules.anpr.application.ports import BufferedEntry, DlqEntry
from parking.modules.devices.application.contracts import BarrierInfo
from parking.platform import metrics

OFFLINE_STREAM = "anpr:offline"
OFFLINE_DLQ = "anpr:dlq"
OFFLINE_GROUP = "offline"
EVENTS_DLQ = "events:dlq"
_RESPONSE_PREFIX = "anpr:idem:"
_RATE_PREFIX = "ratelimit:"
_BARRIER_PREFIX = "barrier:cache:"


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _decode(fields: Any) -> dict[str, str]:
    return {_text(k): _text(v) for k, v in (fields or {}).items()}


def _entries(raw: Any) -> list[tuple[str, dict[str, str]]]:
    return [(_text(eid), _decode(fields)) for eid, fields in (raw or [])]


def _fields(fields: dict[str, str]) -> dict[Any, Any]:
    return dict(fields)


class RedisResponseCache:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def get(self, key: bytes) -> dict[str, Any] | None:
        raw = await self._r.get(_RESPONSE_PREFIX + key.hex())
        result: dict[str, Any] | None = None if raw is None else json.loads(raw)
        return result

    async def set(self, key: bytes, response: dict[str, Any], ttl_seconds: int) -> None:
        await self._r.set(_RESPONSE_PREFIX + key.hex(), json.dumps(response), ex=ttl_seconds)


class RedisRateLimiter:
    """Скользящее окно на ZSET: ZADD(now) → ZREMRANGEBYSCORE(< now − window) → ZCARD → EXPIRE, одна MULTI."""

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def hit(self, subject: str, *, limit: int, window_seconds: int) -> int | None:
        key = _RATE_PREFIX + subject
        now = time.time()
        pipe = self._r.pipeline(transaction=True)
        pipe.zadd(key, {f"{now:.6f}": now})
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zcard(key)
        pipe.zrange(key, 0, 0, withscores=True)
        pipe.expire(key, window_seconds)
        _, _, count, oldest, _ = await pipe.execute()
        if int(count) <= limit:
            return None
        oldest_ts = float(oldest[0][1]) if oldest else now
        return max(1, math.ceil(oldest_ts + window_seconds - now))


class RedisOfflineBuffer:
    def __init__(
        self, redis: Redis, *, maxlen_cap: int, stream: str = OFFLINE_STREAM, group: str = OFFLINE_GROUP
    ) -> None:
        self._r = redis
        self._cap = maxlen_cap
        self._stream = stream
        self._group = group

    async def enqueue(self, fields: dict[str, str]) -> str:
        entry_id = await self._r.xadd(self._stream, _fields(fields), maxlen=self._cap, approximate=True)
        return _text(entry_id)

    async def length(self) -> int:
        return int(await self._r.xlen(self._stream))

    async def ensure_group(self) -> None:
        try:
            await self._r.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, consumer: str, *, count: int, own_pending: bool) -> list[BufferedEntry]:
        response: Any = await self._r.xreadgroup(
            self._group, consumer, {self._stream: "0" if own_pending else ">"}, count=count
        )  # без BLOCK: цикл сам делает паузы, socket_timeout 0.2 с не должен срабатывать на ожидании
        return [
            BufferedEntry(entry_id=eid, fields=fields)
            for _stream, entries in response or []
            for eid, fields in _entries(entries)
        ]

    async def claim_stale(self, consumer: str, *, min_idle_ms: int, count: int) -> list[BufferedEntry]:
        _, claimed, _ = await self._r.xautoclaim(
            self._stream, self._group, consumer, min_idle_time=min_idle_ms, count=count
        )
        return [BufferedEntry(entry_id=eid, fields=fields) for eid, fields in _entries(claimed)]

    async def ack(self, entry_ids: Sequence[str]) -> None:
        """ACK + XDEL: обработанное уже в PostgreSQL, а XLEN должен отражать невоспроизведённый хвост (порог 503)."""
        if entry_ids:
            pipe = self._r.pipeline(transaction=True)
            pipe.xack(self._stream, self._group, *entry_ids)
            pipe.xdel(self._stream, *entry_ids)
            await pipe.execute()

    async def to_dlq(self, entry: BufferedEntry, error: str) -> None:
        fields = {**entry.fields, "error": error[:500], "source_stream": self._stream, "source_id": entry.entry_id}
        await self._r.xadd(OFFLINE_DLQ, _fields(fields))
        metrics.consumer_dlq_total.labels(stream=self._stream).inc()

    async def pending(self) -> int:
        try:
            info = await self._r.xpending(self._stream, self._group)
        except ResponseError:
            return 0
        return int(info["pending"])


class RedisDlqStore:
    """DLQ-стримы: anpr:dlq (оффлайн-буфер) и events:dlq (интеграционные события). Replay возвращает запись
    в исходный стрим (source_stream) с обнулённым счётчиком попыток и удаляет её из DLQ."""

    STREAMS: ClassVar[dict[str, str]] = {"anpr": OFFLINE_DLQ, "events": EVENTS_DLQ}

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def entries(self, stream: str, *, limit: int) -> list[DlqEntry]:
        name = self.STREAMS[stream]
        return [
            DlqEntry(entry_id=eid, stream=stream, fields=fields)
            for eid, fields in _entries(await self._r.xrange(name, count=limit))
        ]

    async def replay(self, stream: str, entry_ids: Sequence[str] | None, *, dry_run: bool) -> list[DlqEntry]:
        name = self.STREAMS[stream]
        entries: list[tuple[str, dict[str, str]]] = []
        if entry_ids is None:
            entries = _entries(await self._r.xrange(name, count=10_000))
        else:
            for eid in entry_ids:
                entries.extend(_entries(await self._r.xrange(name, min=eid, max=eid, count=1)))
        result: list[DlqEntry] = []
        for eid, fields in entries:
            result.append(DlqEntry(entry_id=eid, stream=stream, fields=fields))
            if dry_run:
                continue
            target = fields.get("source_stream") or (OFFLINE_STREAM if stream == "anpr" else "events:0")
            replayed = {k: v for k, v in fields.items() if k not in {"error", "source_stream", "source_id"}}
            replayed["retry_count"] = "0"
            replayed["attempt"] = "0"
            replayed["replayed_from"] = eid
            pipe = self._r.pipeline(transaction=True)
            pipe.xadd(target, _fields(replayed))
            pipe.xdel(name, eid)
            await pipe.execute()
        return result


class RedisBarrierInfoCache:
    """Шлагбаум камеры для оффлайн-открытия: заполняет исполнитель команд при каждом исполнении."""

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def remember(self, barrier: BarrierInfo) -> None:
        if barrier.camera_id is None:
            return
        payload = {
            "id": str(barrier.id),
            "lot_id": str(barrier.lot_id),
            "driver": barrier.driver,
            "endpoint": barrier.endpoint,
            "auth_secret_ref": barrier.auth_secret_ref,
            "active": barrier.active,
            "camera_id": str(barrier.camera_id),
            "name": barrier.name,
            "allowlist": list(barrier.allowlist),
        }
        await self._r.set(
            f"{_BARRIER_PREFIX}{barrier.lot_id}:{barrier.camera_id}", json.dumps(payload), ex=7 * 24 * 3600
        )

    async def for_camera(self, lot_id: UUID, camera_id: UUID) -> BarrierInfo | None:
        raw = await self._r.get(f"{_BARRIER_PREFIX}{lot_id}:{camera_id}")
        if raw is None:
            return None
        d = json.loads(raw)
        return BarrierInfo(
            id=UUID(d["id"]),
            lot_id=UUID(d["lot_id"]),
            driver=d["driver"],
            endpoint=d["endpoint"],
            auth_secret_ref=d["auth_secret_ref"],
            active=bool(d["active"]),
            camera_id=UUID(d["camera_id"]),
            name=d["name"],
            allowlist=tuple(d["allowlist"]),
        )
