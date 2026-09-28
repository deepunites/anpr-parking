"""API-ключ внешней системы: формат «pk_<префикс 16>_<секрет>», права — явный список (scopes), срок, площадки."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from parking.shared.errors import ValidationError

KEY_PREFIX = "pk_"
_FORMAT = re.compile(r"^pk_([A-Za-z0-9]{16})_([A-Za-z0-9_-]{32,64})$")
_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Что можно доверить внешней системе: чтение и две операции записи (правила доступа из CRM, открытие стрелы
# домофоном/охраной). Деньги (касса, возвраты), сотрудники, ключи камер и настройки — только людям в панели.
GRANTABLE_SCOPES: tuple[str, ...] = (
    "sessions:read",
    "lots:read",
    "payment:read",
    "access:read",
    "access:write",
    "cameras:read",
    "barriers:read",
    "barrier:open",
    "tariff:read",
    "reports:read",
    "dashboard:view",
)


@dataclass(frozen=True, slots=True)
class IssuedSecret:
    raw: str
    prefix: str


def generate_key() -> IssuedSecret:
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return IssuedSecret(raw=f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}", prefix=prefix)


def split_prefix(raw: str) -> str | None:
    match = _FORMAT.match(raw.strip())
    return match.group(1) if match else None


def looks_like_api_key(token: str) -> bool:
    return token.startswith(KEY_PREFIX)


def check_scopes(scopes: list[str], allowed_for_creator: set[str]) -> tuple[str, ...]:
    """Права ключа: непустой список из GRANTABLE_SCOPES и не шире прав того, кто выпускает ключ."""
    unique = tuple(dict.fromkeys(scopes))
    if not unique:
        raise ValidationError("at least one scope is required", code="SCOPES_REQUIRED")
    bad = [s for s in unique if s not in GRANTABLE_SCOPES]
    if bad:
        raise ValidationError(f"scope cannot be granted to an API key: {bad[0]}", code="SCOPE_NOT_GRANTABLE")
    beyond = [s for s in unique if s not in allowed_for_creator]
    if beyond:
        raise ValidationError(f"scope exceeds your own permissions: {beyond[0]}", code="SCOPE_NOT_GRANTABLE")
    return unique


@dataclass(slots=True)
class ApiKey:
    id: UUID
    tenant_id: UUID
    name: str
    prefix: str
    scopes: tuple[str, ...]
    created_by: UUID
    created_at: datetime
    lot_scope: frozenset[UUID] = field(default_factory=frozenset)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    def usable(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)
