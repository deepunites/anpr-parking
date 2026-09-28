"""Async circuit breaker (замена purgatory — A-26): CLOSED → OPEN после N отказов → HALF_OPEN через ttl → одна проба.

Один экземпляр на ресурс (пул БД, конкретный шлагбаум, провайдер). Без глобального состояния между процессами —
каждый процесс наблюдает отказы сам; это намеренно (иначе один битый под откроет breaker у всех).
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from parking.platform.logging import get_logger

log = get_logger("breaker")


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"circuit {name!r} is open; retry after {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


@dataclass
class CircuitBreaker:
    name: str
    threshold: int = 5
    ttl_seconds: float = 60.0
    monotonic: Callable[[], float] = time.monotonic
    on_state_change: Callable[[str, BreakerState, BreakerState], None] | None = None
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = field(default=0.0)
    _probe_in_flight: bool = field(default=False, repr=False)

    def _set(self, new: BreakerState) -> None:
        if new is self.state:
            return
        old, self.state = self.state, new
        log.warning("breaker_state", breaker=self.name, old=old.value, new=new.value, failures=self.failures)
        if self.on_state_change is not None:
            self.on_state_change(self.name, old, new)

    def acquire(self) -> None:
        """Вызывается перед операцией. Бросает CircuitOpenError, если ресурс закрыт для вызовов."""
        if self.state is BreakerState.OPEN:
            elapsed = self.monotonic() - self.opened_at
            if elapsed < self.ttl_seconds:
                raise CircuitOpenError(self.name, self.ttl_seconds - elapsed)
            self._set(BreakerState.HALF_OPEN)
            self._probe_in_flight = False
        if self.state is BreakerState.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpenError(self.name, 1.0)
            self._probe_in_flight = True

    def record_success(self) -> None:
        self.failures = 0
        self._probe_in_flight = False
        self._set(BreakerState.CLOSED)

    def record_failure(self) -> None:
        self.failures += 1
        self._probe_in_flight = False
        if self.state is BreakerState.HALF_OPEN or self.failures >= self.threshold:
            self.trip()

    def trip(self) -> None:
        """Открыть немедленно: таймаут уже стоил полного бюджета ожидания — считать до threshold бессмысленно."""
        self.failures = max(self.failures, self.threshold)
        self._probe_in_flight = False
        self.opened_at = self.monotonic()
        self._set(BreakerState.OPEN)

    @property
    def is_open(self) -> bool:
        return self.state is BreakerState.OPEN and (self.monotonic() - self.opened_at) < self.ttl_seconds


class BreakerRegistry:
    """Breaker на ключ (например, barrier_id) с общими параметрами."""

    def __init__(
        self,
        *,
        threshold: int = 5,
        ttl_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, BreakerState, BreakerState], None] | None = None,
    ) -> None:
        self._threshold = threshold
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._on_state_change = on_state_change
        self._items: dict[str, CircuitBreaker] = {}

    def get(self, key: str) -> CircuitBreaker:
        breaker = self._items.get(key)
        if breaker is None:
            breaker = self._items[key] = CircuitBreaker(
                key, self._threshold, self._ttl, self._monotonic, on_state_change=self._on_state_change
            )
        return breaker
