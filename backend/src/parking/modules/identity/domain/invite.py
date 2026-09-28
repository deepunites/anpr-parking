"""Приглашение сотрудника по ссылке: 48 часов, одноразовое, отзываемое. Токен в БД не хранится — только его sha256."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from parking.modules.identity.domain.permissions import Role
from parking.shared.errors import ConflictError

INVITE_TTL = timedelta(hours=48)


class InviteStatus(enum.StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


@dataclass(slots=True)
class Invite:
    id: UUID
    tenant_id: UUID
    email: str | None
    phone: str | None
    role: Role
    display_name: str
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    lot_scope: frozenset[UUID] = field(default_factory=frozenset)
    accepted_at: datetime | None = None
    accepted_user_id: UUID | None = None
    revoked_at: datetime | None = None

    def status(self, now: datetime) -> InviteStatus:
        if self.accepted_at is not None:
            return InviteStatus.ACCEPTED
        if self.revoked_at is not None:
            return InviteStatus.REVOKED
        if self.expires_at <= now:
            return InviteStatus.EXPIRED
        return InviteStatus.PENDING

    def ensure_pending(self, now: datetime) -> None:
        status = self.status(now)
        if status is not InviteStatus.PENDING:
            raise ConflictError(f"invite is {status.value.lower()}", code=f"INVITE_{status.value}")

    def accept(self, user_id: UUID, now: datetime) -> None:
        self.ensure_pending(now)
        self.accepted_at = now
        self.accepted_user_id = user_id

    def revoke(self, now: datetime) -> None:
        self.ensure_pending(now)
        self.revoked_at = now
