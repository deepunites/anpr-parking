"""Исходящие вебхуки: каталог событий (внутреннее событие → публичное имя), расписание повторов, подпись, адрес.

Событие «тонкое»: идентификаторы, время, суммы и статусы — без номеров машин и персональных данных; подробности
внешняя система запрашивает API-ключом (GET /sessions/{id})."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from parking.shared.errors import ValidationError

# публичное имя → внутренний тип события (EVENTS.md)
EVENT_CATALOG: dict[str, str] = {
    "session.created": "parking.session.created.v1",
    "session.closed": "parking.session.closed.v1",
    "session.review": "parking.session.manual_review.v1",
    "payment.confirmed": "billing.payment.confirmed.v1",
    "barrier.command_completed": "devices.barrier.command_completed.v1",
    "camera.status_changed": "devices.camera.status_changed.v1",
}
PUBLIC_NAME = {internal: public for public, internal in EVENT_CATALOG.items()}
TEST_EVENT = "webhook.test"
AUTO_DISABLE_AFTER = timedelta(days=3)  # адрес без единой успешной доставки 3 суток — выключается
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))  # после 4-й неудачи — DEAD
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
_METADATA = ipaddress.ip_address("169.254.169.254")


class DeliveryStatus:
    PENDING = "PENDING"
    RETRY = "RETRY"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"


def check_events(events: list[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(events))
    if not unique:
        raise ValidationError("choose at least one event", code="EVENTS_REQUIRED")
    unknown = [e for e in unique if e not in EVENT_CATALOG]
    if unknown:
        raise ValidationError(f"unknown webhook event: {unknown[0]}", code="UNKNOWN_EVENT")
    return unique


def check_url(url: str, *, allow_http: bool) -> str:
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValidationError("webhook URL must use https", code="INVALID_WEBHOOK_URL")
    if not parts.hostname or parts.username or parts.password or len(value) > 500:
        raise ValidationError("webhook URL must have a host and no credentials", code="INVALID_WEBHOOK_URL")
    return value


def check_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool) -> None:
    """Адрес получателя: только публичный интернет (SSRF). allow_private — стенд и локальные интеграции."""
    if ip == _METADATA or ip.is_unspecified or ip.is_multicast:
        raise ValidationError("webhook address is not allowed", code="WEBHOOK_ADDRESS_BLOCKED")
    if allow_private:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or not ip.is_global:
        raise ValidationError("webhook address is not public", code="WEBHOOK_ADDRESS_BLOCKED")


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    """X-Parking-Signature: sha256=HMAC(secret, "<timestamp>.<body>") — получатель сверяет и отвергает старые."""
    digest = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def next_attempt(attempts_done: int, now: datetime) -> datetime | None:
    """attempts_done — сколько попыток уже было (включая только что неудачную). None — пора в DEAD."""
    if attempts_done >= MAX_ATTEMPTS:
        return None
    return now + RETRY_DELAYS[attempts_done - 1]


def envelope(
    *,
    delivery_id: UUID,
    public_event: str,
    event_id: UUID,
    occurred_at: datetime,
    tenant_id: UUID,
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": str(delivery_id),
        "event": public_event,
        "event_id": str(event_id),
        "occurred_at": occurred_at.isoformat(),
        "tenant_id": str(tenant_id),
        "data": data,
    }


@dataclass(slots=True)
class WebhookEndpoint:
    id: UUID
    tenant_id: UUID
    url: str
    events: tuple[str, ...]
    secret_hint: str
    active: bool
    created_by: UUID
    created_at: datetime
    description: str | None = None
    consecutive_failures: int = 0
    disabled_reason: str | None = None  # AUTO_FAILING_3D — выключен диспетчером (3 суток без ответа)


@dataclass(slots=True)
class Delivery:
    id: UUID
    tenant_id: UUID
    endpoint_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]
    status: str
    attempts: int
    created_at: datetime
    next_attempt_at: datetime | None = None
    last_status: int | None = None
    last_error: str | None = None
    last_latency_ms: int | None = None
    delivered_at: datetime | None = None
