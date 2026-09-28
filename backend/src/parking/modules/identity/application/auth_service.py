"""Логин, refresh с ротацией и детекцией reuse, logout, проверка access-токена.

Правила: ответ при неверном пароле и несуществующем пользователе неразличим по коду, тексту и времени
(argon2 выполняется всегда); экспоненциальная задержка по (email, ip) и по ip; блокировка после N попыток с аудитом.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser, TokenPair
from parking.modules.identity.application.ports import (
    AccessTokenIssuer,
    InviteRepository,
    LoginAttemptRepository,
    PasswordHasher,
    RefreshTokenRepository,
    UserRepository,
)
from parking.modules.identity.domain.contact import normalize_login
from parking.modules.identity.domain.permissions import Role
from parking.modules.identity.domain.refresh import RefreshOutcome
from parking.modules.identity.domain.user import LoginLockedOut
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import AuthenticationError, RateLimitedError

log = get_logger("identity.auth")

THROTTLE_WINDOW = timedelta(minutes=15)
PAIR_FREE_ATTEMPTS = 3
IP_FREE_ATTEMPTS = 20
MAX_DELAY_SECONDS = 60
INVALID_CREDENTIALS = "invalid credentials"


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityRepositories:
    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    audit: AuditWriter
    invites: InviteRepository


UowFactory = Callable[[UUID | None], UnitOfWork]
ReposFactory = Callable[[UnitOfWork], IdentityRepositories]


def email_hash(email: str) -> bytes:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).digest()


def _split_refresh(token: str) -> tuple[UUID, bytes]:
    tenant_part, _, secret = token.partition(".")
    if not secret:
        raise AuthenticationError(INVALID_CREDENTIALS)
    try:
        tenant_id = UUID(tenant_part)
    except ValueError:
        raise AuthenticationError(INVALID_CREDENTIALS) from None
    return tenant_id, hashlib.sha256(secret.encode("utf-8")).digest()


class AuthService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repos: ReposFactory,
        attempts: LoginAttemptRepository,
        hasher: PasswordHasher,
        tokens: AccessTokenIssuer,
        clock: Clock,
        refresh_ttl: timedelta,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._attempts = attempts
        self._hasher = hasher
        self._tokens = tokens
        self._clock = clock
        self._refresh_ttl = refresh_ttl
        self._sleep = sleep

    # --- login ------------------------------------------------------------
    async def login(self, login: str, password: str, client: ClientInfo) -> TokenPair:
        """login — e-mail или телефон (v2 этап 4). Мусор вместо логина неотличим от несуществующего пользователя."""
        normalized = normalize_login(login)
        ehash = email_hash(normalized or login)
        await self._throttle(ehash, client.ip)
        now = self._clock.now()
        lookup = None
        if normalized is not None:
            async with self._uow_factory(None) as uow:
                users = self._repos(uow).users
                if "@" in normalized:
                    lookup = await users.lookup_by_email(normalized)
                else:
                    lookup = await users.lookup_by_phone(normalized)
        stored_hash = lookup.password_hash if lookup is not None else self._hasher.dummy_hash
        password_ok = self._hasher.verify(stored_hash, password)
        if lookup is None:
            await self._attempts.record(ehash, client.ip, success=False)
            raise AuthenticationError(INVALID_CREDENTIALS)
        failed = False
        async with self._uow_factory(lookup.tenant_id) as uow:
            repos = self._repos(uow)
            user = await repos.users.get_for_update(lookup.user_id)
            if user is None:
                failed = True
            elif not password_ok or not user.can_login(now) or not lookup.tenant_active:
                failed = True
                user.record_failed_login(now)
                await repos.users.save(user)
                for event in user.pull_events():
                    if isinstance(event, LoginLockedOut):
                        await self._record_lockout(uow, repos, user.tenant_id, user.id, event, client)
            else:
                user.record_successful_login(now)
                if self._hasher.needs_rehash(user.password_hash):
                    user.password_hash = self._hasher.hash(password)
                await repos.users.save(user)
                pair = await self._issue_pair(
                    repos, user.id, user.tenant_id, user.role, user.token_version, uuid4(), client
                )
                await repos.audit.record(
                    AuditEntry(
                        tenant_id=user.tenant_id,
                        action="LOGIN",
                        entity_type="user",
                        entity_id=user.id,
                        actor_type=ActorType.USER,
                        actor_id=user.id,
                        user_id=user.id,
                        reason="password login",
                        ip_address=client.ip,
                        user_agent=client.user_agent,
                    )
                )
        # коммит состоялся: неудачная попытка и блокировка сохранены даже при отказе
        if failed or user is None:
            await self._attempts.record(ehash, client.ip, success=False)
            raise AuthenticationError(INVALID_CREDENTIALS)
        await self._attempts.record(ehash, client.ip, success=True)
        log.info("login", user_id=str(user.id), tenant_id=str(user.tenant_id))
        return pair

    async def _record_lockout(
        self,
        uow: UnitOfWork,
        repos: IdentityRepositories,
        tenant_id: UUID,
        user_id: UUID,
        event: LoginLockedOut,
        client: ClientInfo,
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=tenant_id,
                action="LOGIN_LOCKED",
                entity_type="user",
                entity_id=user_id,
                actor_type=ActorType.SYSTEM,
                payload={"locked_until": event.locked_until.isoformat()},
                ip_address=client.ip,
                user_agent=client.user_agent,
            )
        )
        uow.publish(
            IntegrationEvent(
                type="identity.login.locked.v1",
                tenant_id=tenant_id,
                aggregate_type="user",
                aggregate_id=user_id,
                payload={"user_id": str(user_id), "locked_until": event.locked_until.isoformat()},
            )
        )

    async def _throttle(self, ehash: bytes, ip: str | None) -> None:
        pair_failures, ip_failures = await self._attempts.failures(email_hash=ehash, ip=ip, window=THROTTLE_WINDOW)
        delay = 0
        if pair_failures >= PAIR_FREE_ATTEMPTS:
            delay = min(2 ** (pair_failures - PAIR_FREE_ATTEMPTS), MAX_DELAY_SECONDS)
        if ip_failures >= IP_FREE_ATTEMPTS:
            delay = max(delay, MAX_DELAY_SECONDS)
        if delay:
            raise RateLimitedError("too many login attempts", retry_after_seconds=delay)

    async def _issue_pair(
        self,
        repos: IdentityRepositories,
        user_id: UUID,
        tenant_id: UUID,
        role: Role,
        token_version: int,
        family_id: UUID,
        client: ClientInfo,
    ) -> TokenPair:
        now = self._clock.now()
        secret = secrets.token_urlsafe(48)
        token_id = uuid4()
        await repos.refresh_tokens.add(
            token_id=token_id,
            tenant_id=tenant_id,
            user_id=user_id,
            family_id=family_id,
            token_hash=hashlib.sha256(secret.encode("utf-8")).digest(),
            expires_at=now + self._refresh_ttl,
            ip=client.ip,
            user_agent=client.user_agent,
        )
        access, ttl = self._tokens.issue(
            user_id=user_id, tenant_id=tenant_id, role=role.value, token_version=token_version, now=now
        )
        return TokenPair(
            access_token=access,
            access_expires_in=ttl,
            refresh_token=f"{tenant_id}.{secret}",
            refresh_expires_in=int(self._refresh_ttl.total_seconds()),
        )

    # --- refresh ----------------------------------------------------------
    async def refresh(self, refresh_token: str, client: ClientInfo) -> TokenPair:
        tenant_id, token_hash = _split_refresh(refresh_token)
        now = self._clock.now()
        reuse = False
        pair: TokenPair | None = None
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            record = await repos.refresh_tokens.get_for_update(token_hash)
            outcome = record.evaluate(now) if record is not None else None
            if record is not None and outcome is RefreshOutcome.REUSE_DETECTED:
                reuse = True
                revoked = await repos.refresh_tokens.revoke_family(record.family_id, "REUSE", now)
                await repos.audit.record(
                    AuditEntry(
                        tenant_id=tenant_id,
                        action="REFRESH_REUSE_DETECTED",
                        entity_type="user",
                        entity_id=record.user_id,
                        actor_type=ActorType.SYSTEM,
                        payload={"family_id": str(record.family_id), "revoked": revoked},
                        ip_address=client.ip,
                        user_agent=client.user_agent,
                    )
                )
                uow.publish(
                    IntegrationEvent(
                        type="identity.refresh.reuse_detected.v1",
                        tenant_id=tenant_id,
                        aggregate_type="user",
                        aggregate_id=record.user_id,
                        payload={"user_id": str(record.user_id), "family_id": str(record.family_id)},
                    )
                )
                log.warning("refresh_reuse_detected", user_id=str(record.user_id), family_id=str(record.family_id))
            elif record is not None and outcome is RefreshOutcome.ROTATE:
                user = await repos.users.get(record.user_id)
                if user is not None and user.can_login(now):
                    pair = await self._issue_pair(
                        repos, user.id, tenant_id, user.role, user.token_version, record.family_id, client
                    )
                    new_hash = hashlib.sha256(pair.refresh_token.partition(".")[2].encode()).digest()
                    new_record = await repos.refresh_tokens.get_for_update(new_hash)
                    assert new_record is not None  # только что вставлен в этой же транзакции
                    await repos.refresh_tokens.mark_used(record.id, new_record.id, now)
        if reuse or pair is None:
            raise AuthenticationError(INVALID_CREDENTIALS)
        return pair

    async def logout(self, refresh_token: str, client: ClientInfo) -> None:
        """Отзывает всё семейство refresh-токенов (I-15: системное действие по инициативе пользователя, в аудите)."""
        tenant_id, token_hash = _split_refresh(refresh_token)
        now = self._clock.now()
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            record = await repos.refresh_tokens.get_for_update(token_hash)
            if record is None:
                return
            await repos.refresh_tokens.revoke_family(record.family_id, "LOGOUT", now)
            await repos.audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    action="LOGOUT",
                    entity_type="user",
                    entity_id=record.user_id,
                    actor_type=ActorType.USER,
                    actor_id=record.user_id,
                    user_id=record.user_id,
                    reason="logout",
                    ip_address=client.ip,
                    user_agent=client.user_agent,
                )
            )

    # --- access token -----------------------------------------------------
    async def authenticate(self, access_token: str) -> CurrentUser:
        """Проверяет JWT и актуальность token_version/активности — смена роли действует немедленно."""
        claims = self._tokens.verify(access_token, self._clock.now())
        try:
            user_id = UUID(str(claims["sub"]))
            tenant_id = UUID(str(claims["tenant_id"]))
            token_version = int(str(claims["token_version"]))
        except (KeyError, ValueError):
            raise AuthenticationError(INVALID_CREDENTIALS) from None
        async with self._uow_factory(tenant_id) as uow:
            user = await self._repos(uow).users.get(user_id)
        if user is None or not user.active or user.token_version != token_version:
            raise AuthenticationError(INVALID_CREDENTIALS)
        return CurrentUser(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
            lot_scope=user.lot_scope,
            must_change_password=user.must_change_password,
        )
