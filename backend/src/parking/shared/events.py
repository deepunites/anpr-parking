"""Базовый класс доменных событий — часть shared kernel, чтобы domain не зависел от platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Внутреннее событие агрегата. Не покидает модуль; наружу идёт IntegrationEvent (platform.events)."""

    occurred_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
