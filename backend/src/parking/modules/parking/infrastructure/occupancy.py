"""Occupancy как счётчик в Redis (ADR-05): проекция, сверяемая с COUNT(*) раз в 60 с."""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis

from parking.platform.logging import get_logger

log = get_logger("parking.occupancy")


class RedisOccupancy:
    def __init__(self, redis: Redis, *, ttl_seconds: int = 900) -> None:
        self._r = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(lot_id: UUID) -> str:
        return f"occ:{lot_id}"

    async def get(self, lot_id: UUID) -> int | None:
        try:
            raw = await self._r.get(self._key(lot_id))
        except Exception as exc:
            log.warning("occupancy_cache_unavailable", error_type=type(exc).__name__)
            return None
        return None if raw is None else max(0, int(raw))

    async def adjust(self, lot_id: UUID, delta: int) -> None:
        try:
            pipe = self._r.pipeline(transaction=False)
            pipe.incrby(self._key(lot_id), delta)
            pipe.expire(self._key(lot_id), self._ttl)
            await pipe.execute()
        except Exception as exc:
            log.warning("occupancy_cache_unavailable", error_type=type(exc).__name__)

    async def set(self, lot_id: UUID, value: int) -> None:
        try:
            await self._r.set(self._key(lot_id), value, ex=self._ttl)
        except Exception as exc:
            log.warning("occupancy_cache_unavailable", error_type=type(exc).__name__)
