"""Что identity отдаёт другим модулям и API: аутентифицированный принципал и его права."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from parking.modules.identity.domain.permissions import Permission, Role, permissions_for
from parking.modules.identity.domain.user import User
from parking.platform.db.uow import UnitOfWork

__all__ = ["CurrentUser", "Permission", "Role", "TokenPair", "UserView", "permissions_for"]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: UUID
    tenant_id: UUID
    role: Role
    lot_scope: frozenset[UUID]  # пусто — все площадки tenant
    must_change_password: bool = False  # v2 этап 4: до смены временного пароля доступны только /auth/*
    scopes: frozenset[Permission] | None = None  # v2 этап 6: API-ключ — права только из его scopes, не из роли
    api_key_name: str | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        return self.scopes if self.scopes is not None else permissions_for(self.role)

    @property
    def is_api_key(self) -> bool:
        return self.scopes is not None

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def allowed_lots(self) -> frozenset[UUID] | None:
        """None — без ограничения по площадкам; применяется в репозиториях, не в роутах."""
        return self.lot_scope or None


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_in: int
    refresh_token: str  # формат "<tenant_id>.<secret>" — tenant не секрет, нужен для RLS-контекста до чтения токена
    refresh_expires_in: int


@dataclass(frozen=True, slots=True)
class UserView:
    """Read-DTO пользователя для API и других модулей."""

    id: UUID
    email: str | None
    role: Role
    display_name: str
    locale: str
    active: bool
    lot_scope: frozenset[UUID]
    created_at: datetime | None
    phone: str | None = None
    must_change_password: bool = False
    last_login_at: datetime | None = None

    @classmethod
    def of(cls, user: User) -> UserView:
        return cls(
            id=user.id,
            email=user.email,
            role=user.role,
            display_name=user.display_name,
            locale=user.locale,
            active=user.active,
            lot_scope=user.lot_scope,
            created_at=user.created_at,
            phone=user.phone,
            must_change_password=user.must_change_password,
            last_login_at=user.last_login_at,
        )


class AccountProvisioner(Protocol):
    """v2 этап 3: первый администратор нового клиента при самостоятельной регистрации (в UoW, создающем tenant)."""

    def prepare_password(self, password: str) -> str:
        """Проверка длины и argon2id-хеш — чтобы заявка на регистрацию не хранила пароль в открытом виде."""
        ...

    async def email_taken(self, email: str) -> bool: ...

    async def provision_first_admin_in(
        self, uow: UnitOfWork, tenant_id: UUID, *, email: str, password_hash: str, display_name: str, locale: str
    ) -> UserView: ...
