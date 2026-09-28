"""Redis-буфер недоставленных кадров: hash media:pending (key → jpeg) + список порядка; TTL на весь буфер обновляется
при каждой записи. Объём ограничен maxlen: при переполнении старые кадры теряются (лог + метрика), событие — нет."""

from __future__ import annotations

from redis.asyncio import Redis

from parking.platform.logging import get_logger

log = get_logger("media.pending")
HASH = "media:pending"
ORDER = "media:pending:order"


class RedisPendingFrames:
    def __init__(self, redis: Redis, *, maxlen: int = 5000) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def put(self, key: str, data: bytes, *, ttl_seconds: int) -> None:
        pipe = self._redis.pipeline()
        pipe.hset(HASH, key, data)
        pipe.rpush(ORDER, key)
        pipe.expire(HASH, ttl_seconds)
        pipe.expire(ORDER, ttl_seconds)
        pipe.llen(ORDER)
        results = await pipe.execute()
        if int(results[-1]) > self._maxlen:
            dropped = await self._redis.lpop(ORDER)
            if dropped and not isinstance(dropped, list):
                name = dropped.decode() if isinstance(dropped, bytes) else str(dropped)
                await self._redis.hdel(HASH, name)
                log.error("media_pending_overflow", dropped_key=name)

    async def pop_batch(self, limit: int) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        for _ in range(limit):
            key = await self._redis.lpop(ORDER)
            if not key or isinstance(key, list):
                break
            name = key.decode() if isinstance(key, bytes) else str(key)
            data = await self._redis.hget(HASH, name)
            await self._redis.hdel(HASH, name)
            if data:
                out.append((name, data if isinstance(data, bytes) else str(data).encode()))
        return out

    async def requeue(self, key: str, data: bytes, *, ttl_seconds: int) -> None:
        await self.put(key, data, ttl_seconds=ttl_seconds)

    async def length(self) -> int:
        return int(await self._redis.llen(ORDER))
