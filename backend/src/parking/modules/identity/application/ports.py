"""Порты identity. Реализации — в infrastructure; fake — только в tests/."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from parking.modules.identity.domain.invite import Invite
from parking.modules.identity.domain.refresh import RefreshTokenRecord
from parking.modules.identity.domain.user import User


@dataclass(frozen=True, slots=True)
class UserLookup:
    """Результат pre-tenant поиска по email (SECURITY DEFINER функция) — без tenant-контекста."""

    user_id: UUID
    tenant_id: UUID
    password_hash: str
    active: bool
    tenant_active: bool


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password_hash: str, password: str) -> bool:
        """False при несовпадении; не бросает на битом хеше — это тоже «неверные данные»."""
        ...

    def needs_rehash(self, password_hash: str) -> bool: ...
    @property
    def dummy_hash(self) -> str:
        """Хеш для выравнивания времени ответа, когда пользователь не найден."""
        ...


class AccessTokenIssuer(Protocol):
    def issue(self, *, user_id: UUID, tenant_id: UUID, role: str, token_version: int, now: datetime) -> tuple[str, int]:
        """(jwt, ttl_seconds)."""
        ...

    def verify(self, token: str, now: datetime) -> dict[str, object]:
        """Claims проверенного токена. Бросает AuthenticationError."""
        ...


class UserRepository(Protocol):
    async def lookup_by_email(self, email: str) -> UserLookup | None: ...
    async def lookup_by_phone(self, phone: str) -> UserLookup | None:
        """v2 этап 4: pre-tenant поиск по телефону (E.164)."""
        ...

    async def get(self, user_id: UUID) -> User | None:
        """Внутри tenant-контекста UoW."""
        ...

    async def get_for_update(self, user_id: UUID) -> User | None: ...
    async def add(self, user: User) -> None: ...
    async def save(self, user: User) -> None: ...
    async def list(self, *, limit: int, after: tuple[datetime, UUID] | None) -> list[User]: ...
    async def replace_lot_scope(self, user_id: UUID, lots: frozenset[UUID]) -> None: ...
    async def check_lots(self, lots: frozenset[UUID]) -> None: ...


class RefreshTokenRepository(Protocol):
    async def add(
        self,
        *,
        token_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        family_id: UUID,
        token_hash: bytes,
        expires_at: datetime,
        ip: str | None,
        user_agent: str | None,
    ) -> None: ...
    async def get_for_update(self, token_hash: bytes) -> RefreshTokenRecord | None: ...
    async def mark_used(self, token_id: UUID, replaced_by: UUID, now: datetime) -> None: ...
    async def revoke_family(self, family_id: UUID, reason: str, now: datetime) -> int: ...
    async def revoke_user(self, user_id: UUID, reason: str, now: datetime) -> int:
        """Все живые refresh-токены сотрудника (смена/сброс пароля): refresh не проверяет token_version."""
        ...


class InviteRepository(Protocol):
    async def add(self, invite: Invite, token_hash: bytes) -> None: ...
    async def get_for_update(self, invite_id: UUID) -> Invite | None: ...
    async def save(self, invite: Invite) -> None: ...
    async def list(self, *, limit: int) -> list[Invite]: ...
    async def lookup_by_token(self, token_hash: bytes) -> tuple[UUID, UUID] | None:
        """(invite_id, tenant_id) без tenant-контекста (SECURITY DEFINER функция)."""
        ...


class LoginAttemptRepository(Protocol):
    async def record(self, email_hash: bytes, ip: str | None, success: bool) -> None: ...
    async def failures(self, *, email_hash: bytes, ip: str | None, window: timedelta) -> tuple[int, int]:
        """(по паре email+ip, по ip) за окно."""
        ...
