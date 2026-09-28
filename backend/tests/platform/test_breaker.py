"""Circuit breaker: 5 отказов → OPEN, через ttl → HALF_OPEN одна проба, успех → CLOSED, отказ пробы → снова OPEN."""

from __future__ import annotations

import pytest

from parking.platform.breaker import BreakerState, CircuitBreaker, CircuitOpenError


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_state_machine() -> None:
    clock = Clock()
    transitions: list[tuple[str, str]] = []
    b = CircuitBreaker(
        "barrier-1",
        threshold=5,
        ttl_seconds=60,
        monotonic=clock,
        on_state_change=lambda n, o, new: transitions.append((o.value, new.value)),
    )
    for _ in range(4):
        b.acquire()
        b.record_failure()
    assert b.state is BreakerState.CLOSED
    b.acquire()
    b.record_failure()
    assert b.state is BreakerState.OPEN and b.is_open
    with pytest.raises(CircuitOpenError) as exc:
        b.acquire()
    assert exc.value.retry_after == pytest.approx(60.0)
    clock.t = 61
    b.acquire()  # проба
    assert b.state is BreakerState.HALF_OPEN
    with pytest.raises(CircuitOpenError):
        b.acquire()  # вторая проба одновременно не допускается
    b.record_failure()
    assert b.state is BreakerState.OPEN
    clock.t = 130
    b.acquire()
    b.record_success()
    assert b.state is BreakerState.CLOSED and b.failures == 0
    assert transitions == [
        ("closed", "open"),
        ("open", "half_open"),
        ("half_open", "open"),
        ("open", "half_open"),
        ("half_open", "closed"),
    ]


def test_success_resets_counter() -> None:
    b = CircuitBreaker("x", threshold=3)
    for _ in range(2):
        b.record_failure()
    b.record_success()
    for _ in range(2):
        b.record_failure()
    assert b.state is BreakerState.CLOSED
