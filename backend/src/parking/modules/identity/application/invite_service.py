"""v2 этап 4: приглашение сотрудника по ссылке. Администратор указывает имя, роль, площадки и e-mail или телефон;
получает ссылку (один раз) и отправляет её сотруднику сам или каналом D-3 (A-76). Сотрудник открывает ссылку,
задаёт свой пароль — учётка создаётся сразу с его паролем, временного пароля нет."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry
from parking.modules.identity.application.auth_service import IdentityRepositories, ReposFactory, UowFactory
from parking.modules.identity.application.contracts import CurrentUser, UserView
from parking.modules.identity.application.ports import PasswordHasher
from parking.modules.identity.application.user_service import (
    check_password,
    contacts,
    ensure_can_grant,
    ensure_logins_free,
    ensure_lots_within_actor,
)
from parking.modules.identity.domain.invite import INVITE_TTL, Invite, InviteStatus
from parking.modules.identity.domain.permissions import Role
from parking.modules.identity.domain.user import User
from parking.shared.clock import Clock
from parking.shared.errors import NotFoundError

MAX_LIST = 100


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class InviteCommand:
    display_name: str
    role: Role
    email: str | None = None
    phone: str | None = None
    lot_scope: frozenset[UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class InviteView:
    id: UUID
    display_name: str
    role: Role
    email: str | None
    phone: str | None
    lot_scope: frozenset[UUID]
    status: InviteStatus
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None

    @classmethod
    def of(cls, invite: Invite, now: datetime) -> InviteView:
        return cls(
            id=invite.id,
            display_name=invite.display_name,
            role=invite.role,
            email=invite.email,
            phone=invite.phone,
            lot_scope=invite.lot_scope,
            status=invite.status(now),
            created_at=invite.created_at,
            expires_at=invite.expires_at,
            accepted_at=invite.accepted_at,
        )


@dataclass(frozen=True, slots=True)
class IssuedInvite:
    invite: InviteView
    token: str  # показывается администратору один раз; в БД — только sha256


@dataclass(frozen=True, slots=True)
class InvitePreview:
    """Что видит сотрудник по ссылке — без данных клиента, кроме его id (название подставляет API)."""

    tenant_id: UUID
    display_name: str
    role: Role
    login: str  # e-mail или телефон, под которым он будет входить
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AcceptedInvite:
    user: UserView
    login: str


class InviteService:
    def __init__(self, *, uow_factory: UowFactory, repos: ReposFactory, hasher: PasswordHasher, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._hasher = hasher
        self._clock = clock

    async def create(self, actor: CurrentUser, cmd: InviteCommand, *, reason: str) -> IssuedInvite:
        ensure_can_grant(actor, cmd.role)
        ensure_lots_within_actor(actor, cmd.lot_scope)
        email, phone = contacts(cmd.email, cmd.phone)
        now = self._clock.now()
        token = secrets.token_urlsafe(32)
        invite = Invite(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            email=email,
            phone=phone,
            role=cmd.role,
            display_name=cmd.display_name.strip(),
            lot_scope=cmd.lot_scope,
            created_by=actor.user_id,
            created_at=now,
            expires_at=now + INVITE_TTL,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await ensure_logins_free(repos, email, phone)
            if cmd.lot_scope:  # площадки своего клиента (RLS): проверяем тем же путём, что и при создании сотрудника
                await _check_lots(repos, cmd.lot_scope)
            await repos.invites.add(invite, token_hash(token))
            await self._audit(repos, actor, "USER_INVITED", invite.id, reason, {"role": cmd.role.value})
        return IssuedInvite(invite=InviteView.of(invite, now), token=token)

    async def list(self, actor: CurrentUser) -> list[InviteView]:
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            return [InviteView.of(i, now) for i in await self._repos(uow).invites.list(limit=MAX_LIST)]

    async def revoke(self, actor: CurrentUser, invite_id: UUID, *, reason: str) -> None:
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            invite = await repos.invites.get_for_update(invite_id)
            if invite is None:
                raise NotFoundError("invite not found")
            invite.revoke(now)
            await repos.invites.save(invite)
            await self._audit(repos, actor, "INVITE_REVOKED", invite.id, reason, {})

    async def preview(self, token: str) -> InvitePreview:
        invite = await self._load(token)
        invite.ensure_pending(self._clock.now())
        return InvitePreview(
            tenant_id=invite.tenant_id,
            display_name=invite.display_name,
            role=invite.role,
            login=invite.email or invite.phone or "",
            expires_at=invite.expires_at,
        )

    async def accept(self, token: str, *, password: str, display_name: str | None, locale: str) -> AcceptedInvite:
        check_password(password)
        password_hash = self._hasher.hash(password)  # argon2 — до транзакции, не держим блокировку строки
        lookup = await self._lookup(token)
        now = self._clock.now()
        async with self._uow_factory(lookup[1]) as uow:
            repos = self._repos(uow)
            invite = await repos.invites.get_for_update(lookup[0])
            if invite is None:
                raise NotFoundError("invite not found", code="INVITE_NOT_FOUND")
            invite.ensure_pending(now)
            await ensure_logins_free(repos, invite.email, invite.phone)
            user = User(
                id=uuid4(),
                tenant_id=invite.tenant_id,
                email=invite.email,
                phone=invite.phone,
                password_hash=password_hash,
                role=invite.role,
                display_name=(display_name or "").strip() or invite.display_name,
                locale=locale,
                lot_scope=invite.lot_scope,
            )
            await repos.users.add(user)
            await repos.users.replace_lot_scope(user.id, invite.lot_scope)
            invite.accept(user.id, now)
            await repos.invites.save(invite)
            await repos.audit.record(
                AuditEntry(
                    tenant_id=invite.tenant_id,
                    action="INVITE_ACCEPTED",
                    entity_type="user",
                    entity_id=user.id,
                    actor_type=ActorType.USER,
                    actor_id=user.id,
                    user_id=user.id,
                    reason="принято приглашение",
                    payload={
                        "invite_id": str(invite.id),
                        "role": invite.role.value,
                        "invited_by": str(invite.created_by),
                    },
                )
            )
        return AcceptedInvite(user=UserView.of(user), login=invite.email or invite.phone or "")

    async def _lookup(self, token: str) -> tuple[UUID, UUID]:
        async with self._uow_factory(None) as uow:
            found = await self._repos(uow).invites.lookup_by_token(token_hash(token))
        if found is None:
            raise NotFoundError("invite not found", code="INVITE_NOT_FOUND")
        return found

    async def _load(self, token: str) -> Invite:
        invite_id, tenant_id = await self._lookup(token)
        async with self._uow_factory(tenant_id) as uow:
            invite = await self._repos(uow).invites.get_for_update(invite_id)
        if invite is None:
            raise NotFoundError("invite not found", code="INVITE_NOT_FOUND")
        return invite

    @staticmethod
    async def _audit(
        repos: IdentityRepositories,
        actor: CurrentUser,
        action: str,
        invite_id: UUID,
        reason: str,
        payload: dict[str, object],
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="user_invite",
                entity_id=invite_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload=payload,
            )
        )


async def _check_lots(repos: IdentityRepositories, lots: frozenset[UUID]) -> None:
    """Площадки из приглашения существуют у этого клиента — ошибка сейчас, а не при принятии."""
    await repos.users.check_lots(lots)
