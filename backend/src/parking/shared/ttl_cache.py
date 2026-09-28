"""Кеш процесса для редко меняющихся справочных данных (площадка, шлагбаум, версия тарифа).

Стоимость camera-события (долг §4 п.1 PRODUCTION_READINESS): справочники читались из БД на каждое событие. Кеш держит
запись `ttl_seconds` (по умолчанию `reference_cache_ttl_seconds`), поэтому изменение справочника вступает в силу на всех
подах не позже TTL; под, через который изменение сделано, сбрасывает запись сразу (`invalidate`). `ttl_seconds <= 0`
выключает кеш (каждый `get` — промах) — для тестов и отладки.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable


class TtlCache[K: Hashable, V]:
    def __init__(
        self, ttl_seconds: float, *, max_items: int = 10_000, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_items
        self._now = monotonic
        self._items: dict[K, tuple[float, V]] = {}

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: K) -> V | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= self._now():
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: K, value: V) -> None:
        if not self.enabled:
            return
        if len(self._items) >= self._max:  # справочники маленькие; переполнение = утечка ключей, проще сбросить всё
            self._items.clear()
        self._items[key] = (self._now() + self._ttl, value)

    def invalidate(self, key: K) -> None:
        self._items.pop(key, None)

    def invalidate_where(self, predicate: Callable[[K], bool]) -> None:
        for key in [k for k in self._items if predicate(k)]:
            del self._items[key]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
