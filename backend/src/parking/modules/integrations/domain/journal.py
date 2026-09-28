"""Журнал событий камер: номер — только маской (01A***BC): журнал для отладки интеграции, не для поиска машин."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

_CLEAN = re.compile(r"[^0-9A-Za-zА-Яа-я]")


def mask_plate(raw: str | None) -> str | None:
    if not raw:
        return None
    value = _CLEAN.sub("", raw).upper()[:12]
    if len(value) <= 3:
        return "*" * len(value)
    if len(value) <= 5:
        return value[0] + "*" * (len(value) - 1)
    return value[:3] + "*" * (len(value) - 5) + value[-2:]


class Outcome:
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    QUEUED = "QUEUED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    tenant_id: UUID
    source: str
    outcome: str
    received_at: datetime
    lot_id: UUID | None = None
    camera_id: UUID | None = None
    status: str | None = None
    http_status: int | None = None
    event_type: str | None = None
    plate_masked: str | None = None
    message: str | None = None
    correlation_id: UUID | None = None
    latency_ms: int | None = None
