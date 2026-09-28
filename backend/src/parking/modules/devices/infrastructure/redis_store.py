"""Redis-хранилища devices: кеш кандидатов ключей (с отрицательными ответами), heartbeat, last_used, базовая линия."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from parking.modules.devices.application.ports import Heartbeat, KeyCandidate

_AUTH_PREFIX = "camauth:"
_STALE_PREFIX = "camauth:stale:"
_STALE_TTL = 24 * 3600
_FAIL_PREFIX = "camauth:fail:"
_LAST_USED = "camkey:lastused"
_HB_PREFIX = "camera:hb:"
_LAST_EVENT = "camera:lastevent"
_BASELINE_PREFIX = "camera:baseline:"
_EWMA_ALPHA = 0.2


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _dump(c: KeyCandidate) -> dict[str, Any]:
    return {
        "key_id": str(c.key_id),
        "tenant_id": str(c.tenant_id),
        "camera_id": str(c.camera_id),
        "lot_id": str(c.lot_id),
        "key_hmac": c.key_hmac.hex(),
        "pepper_version": c.pepper_version,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "camera_active": c.camera_active,
        "direction": c.direction,
        "code": c.code,
        "confidence_min": c.confidence_min,
        "confidence_review_min": c.confidence_review_min,
        "max_clock_skew_seconds": c.max_clock_skew_seconds,
        "late_event_window_hours": c.late_event_window_hours,
        "tenant_active": c.tenant_active,
    }


def _load(d: dict[str, Any]) -> KeyCandidate:
    return KeyCandidate(
        key_id=UUID(d["key_id"]),
        tenant_id=UUID(d["tenant_id"]),
        camera_id=UUID(d["camera_id"]),
        lot_id=UUID(d["lot_id"]),
        key_hmac=bytes.fromhex(d["key_hmac"]),
        pepper_version=int(d["pepper_version"]),
        expires_at=datetime.fromisoformat(d["expires_at"]) if d["expires_at"] else None,
        camera_active=bool(d["camera_active"]),
        direction=d["direction"],
        code=d["code"],
        confidence_min=int(d["confidence_min"]),
        confidence_review_min=int(d["confidence_review_min"]),
        max_clock_skew_seconds=int(d["max_clock_skew_seconds"]),
        late_event_window_hours=int(d["late_event_window_hours"]),
        tenant_active=bool(d["tenant_active"]),
    )


class RedisCameraAuthCache:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def get(self, key_prefix: str) -> list[KeyCandidate] | None:
        raw = await self._r.get(_AUTH_PREFIX + key_prefix)
        if raw is None:
            return None
        return [_load(d) for d in json.loads(raw)]

    async def get_stale(self, key_prefix: str) -> list[KeyCandidate] | None:
        raw = await self._r.get(_STALE_PREFIX + key_prefix)
        if raw is None:
            return None
        return [_load(d) for d in json.loads(raw)]

    async def set(self, key_prefix: str, candidates: Sequence[KeyCandidate], ttl_seconds: int) -> None:
        payload = json.dumps([_dump(c) for c in candidates])
        pipe = self._r.pipeline(transaction=True)
        pipe.set(_AUTH_PREFIX + key_prefix, payload, ex=ttl_seconds)
        pipe.set(_STALE_PREFIX + key_prefix, payload, ex=_STALE_TTL)
        await pipe.execute()

    async def invalidate(self, key_prefix: str) -> None:
        await self._r.delete(_AUTH_PREFIX + key_prefix, _STALE_PREFIX + key_prefix)

    async def touch(self, key_id: UUID, at: datetime) -> None:
        await self._r.hset(_LAST_USED, str(key_id), at.isoformat())

    async def count_failure(self, subject: str, window_seconds: int) -> int:
        key = _FAIL_PREFIX + subject
        pipe = self._r.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count)


class RedisHeartbeatStore:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def record(self, hb: Heartbeat, ttl_seconds: int) -> None:
        key = _HB_PREFIX + str(hb.camera_id)
        pipe = self._r.pipeline(transaction=True)
        pipe.hset(key, mapping={"at": hb.at.isoformat(), "fw": hb.firmware_version or ""})
        pipe.expire(key, ttl_seconds)
        await pipe.execute()

    async def read_many(self, camera_ids: Sequence[UUID]) -> dict[UUID, Heartbeat]:
        if not camera_ids:
            return {}
        pipe = self._r.pipeline(transaction=False)
        for cid in camera_ids:
            pipe.hgetall(_HB_PREFIX + str(cid))
        result: dict[UUID, Heartbeat] = {}
        for cid, raw in zip(camera_ids, await pipe.execute(), strict=True):
            if raw:
                fw = raw.get(b"fw", b"").decode() or None
                result[cid] = Heartbeat(
                    camera_id=cid, at=datetime.fromisoformat(raw[b"at"].decode()), firmware_version=fw
                )
        return result

    async def note_event(self, camera_id: UUID, at: datetime) -> None:
        """EWMA межсобытийного интервала по часу суток: «обычный поток» камеры для детекции SILENT."""
        prev_raw = await self._r.hget(_LAST_EVENT, str(camera_id))
        pipe = self._r.pipeline(transaction=True)
        pipe.hset(_LAST_EVENT, str(camera_id), at.isoformat())
        if prev_raw is not None:
            prev = datetime.fromisoformat(_text(prev_raw))
            gap = max((at - prev).total_seconds(), 0.0)
            if gap > 0:
                base_key = f"{_BASELINE_PREFIX}{camera_id}:{at.astimezone(UTC).hour}"
                old = await self._r.get(base_key)
                ewma = gap if old is None else (1 - _EWMA_ALPHA) * float(old) + _EWMA_ALPHA * gap
                pipe.set(base_key, repr(ewma), ex=14 * 24 * 3600)
        await pipe.execute()

    async def expected_gap_seconds(self, camera_id: UUID, hour: int) -> float | None:
        raw = await self._r.get(f"{_BASELINE_PREFIX}{camera_id}:{hour}")
        return None if raw is None else float(raw)

    async def last_events(self, camera_ids: Sequence[UUID]) -> dict[UUID, datetime]:
        if not camera_ids:
            return {}
        raw = await self._r.hmget(_LAST_EVENT, [str(c) for c in camera_ids])
        return {cid: datetime.fromisoformat(_text(v)) for cid, v in zip(camera_ids, raw, strict=True) if v is not None}

    async def drain_key_usage(self) -> dict[UUID, datetime]:
        pipe = self._r.pipeline(transaction=True)
        pipe.hgetall(_LAST_USED)
        pipe.delete(_LAST_USED)
        raw, _ = await pipe.execute()
        return {UUID(k.decode()): datetime.fromisoformat(v.decode()) for k, v in raw.items()}
