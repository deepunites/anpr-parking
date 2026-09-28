"""Источник времени как порт: домен не вызывает datetime.now() напрямую."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущее время, aware, UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


def ensure_aware(value: datetime) -> datetime:
    """Домен работает только с aware-датами; naive — ошибка программиста, не данных."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return value
