"""Семейства refresh-токенов: ротация при каждом использовании, детекция повторного использования."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class RefreshOutcome(enum.Enum):
    ROTATE = "rotate"
    REUSE_DETECTED = "reuse_detected"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class RefreshTokenRecord:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    family_id: UUID
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None

    def evaluate(self, now: datetime) -> RefreshOutcome:
        """Порядок важен: отозванный → повторно использованный → истёкший → ротация."""
        if self.revoked_at is not None:
            return RefreshOutcome.REVOKED
        if self.used_at is not None:
            return RefreshOutcome.REUSE_DETECTED
        if self.expires_at <= now:
            return RefreshOutcome.EXPIRED
        return RefreshOutcome.ROTATE
