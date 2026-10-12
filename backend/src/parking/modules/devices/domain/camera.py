"""Камера и её API-ключ: сущности без IO. Здоровье камеры считается чистой функцией из heartbeat/событий."""

from __future__ import annotations

import enum
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from parking.shared.errors import ConflictError, ValidationError

KEY_PREFIX_LEN = 16
KEY_SECRET_BYTES = 32  # 256 бит энтропии: перебор невозможен, растягивание (argon2/bcrypt) не нужно
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


class CameraDirection(enum.StrEnum):
    IN = "IN"
    OUT = "OUT"


class CameraVendor(enum.StrEnum):
    GENERIC = "GENERIC"
    HIKVISION = "HIKVISION"
    DAHUA = "DAHUA"


class CameraStatus(enum.StrEnum):
    UNKNOWN = "UNKNOWN"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    SILENT = "SILENT"
    OFFLINE = "OFFLINE"


@dataclass(slots=True, kw_only=True)
class Camera:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    code: str
    name: str
    direction: CameraDirection
    vendor: CameraVendor = CameraVendor.GENERIC
    location: str | None = None
    confidence_min: int = 80
    confidence_review_min: int = 60
    max_clock_skew_seconds: int = 120
    heartbeat_interval_sec: int = 300
    active: bool = True
    status: CameraStatus = CameraStatus.UNKNOWN
    last_heartbeat_at: datetime | None = None
    last_event_at: datetime | None = None
    firmware_version: str | None = None
    video_endpoint: str | None = None  # запись на сервере площадки: http://IP камеры (RTSP — порт 554)
    video_credentials_encrypted: bytes | None = None  # логин/пароль видеопотока, зашифрованы ключом клиента

    def __post_init__(self) -> None:
        if not (0 <= self.confidence_review_min <= self.confidence_min <= 100):
            raise ValidationError("confidence thresholds must satisfy 0 <= review_min <= min <= 100")
        if self.max_clock_skew_seconds <= 0 or self.heartbeat_interval_sec <= 0:
            raise ValidationError("clock skew and heartbeat interval must be positive")

    def update(
        self,
        *,
        name: str | None = None,
        location: str | None = None,
        confidence_min: int | None = None,
        confidence_review_min: int | None = None,
        max_clock_skew_seconds: int | None = None,
        heartbeat_interval_sec: int | None = None,
        active: bool | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        if location is not None:
            self.location = location
        if confidence_min is not None:
            self.confidence_min = confidence_min
        if confidence_review_min is not None:
            self.confidence_review_min = confidence_review_min
        if max_clock_skew_seconds is not None:
            self.max_clock_skew_seconds = max_clock_skew_seconds
        if heartbeat_interval_sec is not None:
            self.heartbeat_interval_sec = heartbeat_interval_sec
        if active is not None:
            self.active = active
        self.__post_init__()

    def set_video(self, endpoint: str | None, credentials_encrypted: bytes | None) -> None:
        """Адрес и логин видеопотока — только вместе; None и None — запись этой камеры выключена."""
        if (endpoint is None) != (credentials_encrypted is None):
            raise ValidationError("camera video address, login and password go together", code="CREDENTIALS_REQUIRED")
        self.video_endpoint = endpoint.strip() if endpoint else None
        self.video_credentials_encrypted = credentials_encrypted


def compute_status(
    *,
    now: datetime,
    interval_sec: int,
    last_heartbeat_at: datetime | None,
    last_event_at: datetime | None,
    expected_event_gap: timedelta | None,
) -> CameraStatus:
    """ONLINE < 2×интервал, DEGRADED < 5×, иначе OFFLINE. SILENT — heartbeat идёт, а событий нет дольше
    базовой линии для этого времени суток (камера жива, но не распознаёт): простой heartbeat это не ловит."""
    if last_heartbeat_at is None:
        return CameraStatus.OFFLINE
    silence = now - last_heartbeat_at
    if silence >= timedelta(seconds=5 * interval_sec):
        return CameraStatus.OFFLINE
    if silence >= timedelta(seconds=2 * interval_sec):
        return CameraStatus.DEGRADED
    if expected_event_gap is not None and (last_event_at is None or now - last_event_at > expected_event_gap):
        return CameraStatus.SILENT
    return CameraStatus.ONLINE


@dataclass(slots=True, kw_only=True)
class CameraApiKey:
    id: UUID
    tenant_id: UUID
    camera_id: UUID
    key_prefix: str
    key_hmac: bytes
    pepper_version: int = 1
    label: str | None = None
    active: bool = True
    created_at: datetime | None = None
    created_by: UUID | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None

    def revoke(self, *, now: datetime, by: UUID | None) -> None:
        if not self.active:
            raise ConflictError("camera key is already revoked")
        self.active = False
        self.revoked_at = now
        self.revoked_by = by

    def expire_after(self, *, now: datetime, grace: timedelta) -> None:
        """Ротация (A-17): старый ключ живёт ещё окно перекрытия, чтобы камеру можно было перенастроить без простоя."""
        if not self.active:
            raise ConflictError("camera key is already revoked")
        candidate = now + grace
        if self.expires_at is None or candidate < self.expires_at:
            self.expires_at = candidate


@dataclass(frozen=True, slots=True)
class IssuedKey:
    key: CameraApiKey
    raw: str = field(repr=False)  # показывается один раз при выпуске; в БД только HMAC


def generate_raw_key() -> str:
    """`<16 символов префикса><43 символа секрета>`: префикс — индекс кандидатов, секрет — 256 бит."""
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(KEY_PREFIX_LEN))
    return prefix + secrets.token_urlsafe(KEY_SECRET_BYTES)[:43]


def split_prefix(raw: str) -> str | None:
    if len(raw) < KEY_PREFIX_LEN + 1 or len(raw) > 128:
        return None
    return raw[:KEY_PREFIX_LEN]


def new_camera_id() -> UUID:
    return uuid4()
