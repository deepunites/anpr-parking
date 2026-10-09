"""Шлагбаум как сущность администрирования; исполнение команд — command.py."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from uuid import UUID

from parking.shared.errors import ValidationError


class BarrierDriverKind(enum.StrEnum):
    MODBUS_TCP = "MODBUS_TCP"
    HTTP_REST = "HTTP_REST"
    HIKVISION_ISAPI = (
        "HIKVISION_ISAPI"  # реле самой ANPR-камеры Hikvision (DS-TCG406-E): команда ISAPI, без отдельного реле
    )


class BarrierState(enum.StrEnum):
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    OPENING = "OPENING"
    CLOSING = "CLOSING"
    ERROR = "ERROR"


@dataclass(slots=True, kw_only=True)
class Barrier:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    name: str
    driver: BarrierDriverKind
    endpoint: str
    camera_id: UUID | None = None
    auth_secret_ref: str | None = None
    auto_close_seconds: int | None = None
    active: bool = True
    state: BarrierState = BarrierState.UNKNOWN
    credentials_encrypted: bytes | None = None  # HIKVISION_ISAPI: логин/пароль камеры, зашифрованы ключом клиента

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise ValidationError("endpoint is required")
        if self.driver is BarrierDriverKind.HTTP_REST and not self.auth_secret_ref:
            raise ValidationError("HTTP_REST barrier requires auth_secret_ref")
        if self.driver is BarrierDriverKind.HIKVISION_ISAPI and not self.credentials_encrypted:
            raise ValidationError(
                "Hikvision camera barrier requires camera login and password", code="CREDENTIALS_REQUIRED"
            )
        if self.auto_close_seconds is not None and self.auto_close_seconds <= 0:
            raise ValidationError("auto_close_seconds must be positive")

    def update(
        self,
        *,
        name: str | None = None,
        endpoint: str | None = None,
        camera_id: UUID | None = None,
        auth_secret_ref: str | None = None,
        auto_close_seconds: int | None = None,
        active: bool | None = None,
        credentials_encrypted: bytes | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        if endpoint is not None:
            self.endpoint = endpoint
        if camera_id is not None:
            self.camera_id = camera_id
        if auth_secret_ref is not None:
            self.auth_secret_ref = auth_secret_ref
        if auto_close_seconds is not None:
            self.auto_close_seconds = auto_close_seconds
        if active is not None:
            self.active = active
        if credentials_encrypted is not None:
            self.credentials_encrypted = credentials_encrypted
        self.__post_init__()
