"""TtlCache: TTL, выключение нулём, сброс по предикату, защита от переполнения."""

from __future__ import annotations

from parking.shared.ttl_cache import TtlCache


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_entry_expires_after_ttl() -> None:
    clock = _Clock()
    cache: TtlCache[str, int] = TtlCache(10.0, monotonic=clock)
    cache.set("a", 1)
    assert cache.get("a") == 1
    clock.t += 9.99
    assert cache.get("a") == 1
    clock.t += 0.01
    assert cache.get("a") is None
    assert len(cache) == 0


def test_zero_ttl_disables_cache() -> None:
    cache: TtlCache[str, int] = TtlCache(0)
    assert not cache.enabled
    cache.set("a", 1)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_invalidate_by_key_and_predicate() -> None:
    cache: TtlCache[tuple[str, int], str] = TtlCache(60.0)
    cache.set(("lot", 1), "x")
    cache.set(("lot", 2), "y")
    cache.set(("other", 1), "z")
    cache.invalidate(("lot", 1))
    assert cache.get(("lot", 1)) is None and cache.get(("lot", 2)) == "y"
    cache.invalidate_where(lambda k: k[0] == "lot")
    assert cache.get(("lot", 2)) is None and cache.get(("other", 1)) == "z"
    cache.clear()
    assert len(cache) == 0


def test_overflow_resets_instead_of_growing() -> None:
    cache: TtlCache[int, int] = TtlCache(60.0, max_items=3)
    for i in range(3):
        cache.set(i, i)
    assert len(cache) == 3
    cache.set(3, 3)  # переполнение: сброс, затем новая запись
    assert len(cache) == 1 and cache.get(3) == 3
