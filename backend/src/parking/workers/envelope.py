"""Конверт события в Redis Stream: плоские поля (все — строки), payload — JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Envelope:
    event_id: UUID
    event_type: str
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    occurred_at: datetime
    correlation_id: str
    trace_parent: str | None
    payload: dict[str, Any]
    attempt: int = 0

    def to_fields(self) -> dict[str, str]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "tenant_id": str(self.tenant_id),
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id),
            "occurred_at": self.occurred_at.isoformat(),
            "correlation_id": self.correlation_id,
            "trace_parent": self.trace_parent or "",
            "payload": json.dumps(self.payload, default=str),
            "attempt": str(self.attempt),
        }

    @classmethod
    def from_fields(cls, fields: dict[bytes, bytes] | dict[str, str]) -> Envelope:
        f = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in fields.items()
        }
        return cls(
            event_id=UUID(f["event_id"]),
            event_type=f["event_type"],
            tenant_id=UUID(f["tenant_id"]),
            aggregate_type=f["aggregate_type"],
            aggregate_id=UUID(f["aggregate_id"]),
            occurred_at=datetime.fromisoformat(f["occurred_at"]),
            correlation_id=f["correlation_id"],
            trace_parent=f.get("trace_parent") or None,
            payload=json.loads(f["payload"]),
            attempt=int(f.get("attempt", "0")),
        )


def shard_for(aggregate_id: UUID, shards: int) -> int:
    return aggregate_id.int % shards


def stream_name(shard: int) -> str:
    return f"events:{shard}"
