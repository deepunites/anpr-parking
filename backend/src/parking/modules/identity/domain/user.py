"""Пользователь: учётные данные, роль, версия токенов, антибрутфорс-состояние."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from parking.modules.identity.domain.permissions import Role
from parking.shared.errors import ValidationError
from parking.shared.events import DomainEvent

LOCKOUT_THRESHOLD = 10
LOCKOUT_DURATION = timedelta(minutes=15)


@dataclass(frozen=True, slots=True, kw_only=True)
class RoleChanged(DomainEvent):
    user_id: UUID
    old_role: Role
    new_role: Role


@dataclass(frozen=True, slots=True, kw_only=True)
class LoginLockedOut(DomainEvent):
    user_id: UUID
    locked_until: datetime


@dataclass(slots=True)
class User:
    id: UUID
    tenant_id: UUID
    email: str | None
    password_hash: str
    role: Role
    display_name: str
    locale: str = "ru"
    token_version: int = 1
    active: bool = True
    failed_logins: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    lot_scope: frozenset[UUID] = field(default_factory=frozenset)
    phone: str | None = None  # v2 этап 4: второй логин (E.164)
    must_change_password: bool = False  # пароль выдан администратором — сменить при первом входе
    events: list[DomainEvent] = field(default_factory=list)

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now

    def can_login(self, now: datetime) -> bool:
        return self.active and not self.is_locked(now)

    def record_failed_login(self, now: datetime) -> None:
        self.failed_logins += 1
        if self.failed_logins >= LOCKOUT_THRESHOLD and not self.is_locked(now):
            self.locked_until = now + LOCKOUT_DURATION
            self.events.append(LoginLockedOut(user_id=self.id, locked_until=self.locked_until))

    def record_successful_login(self, now: datetime) -> None:
        self.failed_logins = 0
        self.locked_until = None
        self.last_login_at = now

    def change_role(self, new_role: Role) -> None:
        """Смена роли действует немедленно: token_version растёт, старые access-токены отвергаются."""
        if new_role is self.role:
            raise ValidationError("role is unchanged")
        old = self.role
        self.role = new_role
        self.token_version += 1
        self.events.append(RoleChanged(user_id=self.id, old_role=old, new_role=new_role))

    def deactivate(self) -> None:
        self.active = False
        self.token_version += 1

    def reactivate(self) -> None:
        """v2: повторное включение сотрудника; token_version растёт — старые сессии в любом случае мертвы."""
        self.active = True
        self.token_version += 1

    def set_password_hash(self, password_hash: str, *, temporary: bool) -> None:
        """v2: новый пароль — все выданные токены отзываются через token_version. temporary — пароль выдал
        администратор (создание, сброс): сотрудник обязан сменить его при первом входе."""
        self.password_hash = password_hash
        self.failed_logins = 0
        self.locked_until = None
        self.must_change_password = temporary
        self.token_version += 1

    def pull_events(self) -> list[DomainEvent]:
        events, self.events = self.events, []
        return events
