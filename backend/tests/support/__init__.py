"""Fake-реализации портов и утилиты — только для тестов (CORE §7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FixedClock:
    """Часы с ручным управлением."""

    def __init__(self, at: datetime | None = None) -> None:
        self._now = at or datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)
