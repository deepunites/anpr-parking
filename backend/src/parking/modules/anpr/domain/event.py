"""Событие камеры как проверенное значение: время, уверенность, идемпотентность — чистые функции (фаза 4 §1)."""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from parking.shared.plate import PlateNumber


class EventType(enum.StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class RejectReason(enum.StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    CAMERA_INACTIVE = "CAMERA_INACTIVE"
    DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
    REPLAY = "REPLAY"
    INVALID_PLATE = "INVALID_PLATE"
    INVALID_IMAGE_KEY = "INVALID_IMAGE_KEY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    BODY_TOO_LARGE = "BODY_TOO_LARGE"


class ConfidenceClass(enum.StrEnum):
    NORMAL = "NORMAL"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class TimeClass:
    skew_seconds: int  # event_time − received_at (со знаком)
    skew_exceeded: bool  # |skew| > camera.max_clock_skew_seconds — помечаем, не отклоняем
    late: bool  # старше late_after, но в окне: сессия задним числом, шлагбаум не трогаем
    replay: bool  # старше окна площадки — отказ


def classify_time(
    event_time: datetime, received_at: datetime, *, max_skew_seconds: int, late_after: timedelta, window: timedelta
) -> TimeClass:
    """Окно replay = `lot.late_event_window_hours` (24 ч, A-13): ретеншен anpr_idempotency 7 суток > окно + skew,
    поэтому дубль внутри окна ловит реестр идемпотентности, а вне окна событие отклоняется здесь — двойная
    обработка невозможна ни в одном интервале."""
    delta = event_time - received_at
    skew = int(delta.total_seconds())
    age = -delta
    return TimeClass(
        skew_seconds=skew,
        skew_exceeded=abs(skew) > max_skew_seconds,
        late=age > late_after and age <= window,
        replay=age > window,
    )


def classify_confidence(confidence: int, *, minimum: int, review_min: int) -> ConfidenceClass:
    if confidence >= minimum:
        return ConfidenceClass.NORMAL
    if confidence >= review_min:
        return ConfidenceClass.REVIEW
    return ConfidenceClass.REJECT


def event_second(event_time: datetime) -> datetime:
    return event_time.replace(microsecond=0)


def idempotency_key(tenant_id: UUID, camera_id: UUID, event_type: EventType, plate_hash: bytes, at: datetime) -> bytes:
    """SHA256(tenant || camera || type || plate_hash || floor(event_time → секунда)) — фаза 4 §1."""
    h = hashlib.sha256()
    h.update(tenant_id.bytes)
    h.update(camera_id.bytes)
    h.update(event_type.value.encode())
    h.update(plate_hash)
    h.update(str(int(event_second(at).timestamp())).encode())
    return h.digest()


def direction_for(event_type: EventType) -> str:
    return "IN" if event_type is EventType.ENTRY else "OUT"


@dataclass(frozen=True, slots=True, kw_only=True)
class CameraEvent:
    """Проверенное событие после аутентификации и валидации; номер — только объект PlateNumber (маскируется в логах)."""

    event_id: UUID
    tenant_id: UUID
    lot_id: UUID
    camera_id: UUID
    camera_code: str
    event_type: EventType
    plate: PlateNumber
    confidence: int
    event_time: datetime
    received_at: datetime
    image_object_key: str | None
    recognition: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    time: TimeClass = TimeClass(0, False, False, False)
    confidence_class: ConfidenceClass = ConfidenceClass.NORMAL
    queued_offline: bool = False
    retry_count: int = 0

    @property
    def direction(self) -> str:
        return direction_for(self.event_type)
