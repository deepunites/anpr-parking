"""Интеграционные события — версионированный контракт наружу (ADR-11). Доменные — parking.shared.events."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from parking.shared.events import DomainEvent

__all__ = ["EVENT_TYPE", "DomainEvent", "IntegrationEvent"]

EVENT_TYPE = re.compile(r"^[a-z_]+\.[a-z_]+\.[a-z_]+\.v[0-9]+$")


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegrationEvent:
    """Публикуемое наружу событие; `type` — `<context>.<aggregate>.<event>.v<N>`, payload — JSON-совместимый."""

    type: str
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not EVENT_TYPE.match(self.type):
            raise ValueError(f"bad integration event type: {self.type}")
