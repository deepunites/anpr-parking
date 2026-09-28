"""Управление пользователями tenant: создание, смена роли (немедленно), деактивация, скоуп площадок;
v2 этап 4: телефон как логин, временный пароль со сменой при первом входе, смена своего пароля."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry
from parking.modules.identity.application.auth_service import IdentityRepositories, ReposFactory, UowFactory
from parking.modules.identity.application.contracts import CurrentUser, UserView
from parking.modules.identity.application.ports import PasswordHasher
from parking.modules.identity.domain.contact import normalize_email, normalize_phone
from parking.modules.identity.domain.permissions import Role, can_grant
from parking.modules.identity.domain.user import RoleChanged, User
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.shared.clock import Clock, SystemClock
from parking.shared.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError

MIN_PASSWORD_LENGTH = 12


@dataclass(frozen=True, slots=True)
class CreateUserCommand:
    email: str | None
    password: str
    role: Role
    display_name: str
    locale: str = "ru"
    lot_scope: frozenset[UUID] = frozenset()
    phone: str | None = None
    temporary_password: bool = False  # пароль придумал администратор — сотрудник сменит его при первом входе


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"password must be at least {MIN_PASSWORD_LENGTH} characters", code="PASSWORD_TOO_SHORT")


def ensure_can_grant(actor: CurrentUser, role: Role) -> None:
    if not can_grant(actor.role, role):
        raise AuthorizationError("platform roles are granted by the platform only", code="ROLE_NOT_GRANTABLE")


def ensure_lots_within_actor(actor: CurrentUser, lots: frozenset[UUID]) -> None:
    """v2 этап 8 (T-26): сотрудник с ограниченными площадками не выдаёт доступ шире своего — пустой набор
    означает «все площадки клиента» и ему недоступен."""
    allowed = actor.allowed_lots()
    if allowed is not None and (not lots or not lots <= allowed):
        raise AuthorizationError("lots must be within your own lots", code="LOTS_OUT_OF_SCOPE")


def ensure_can_manage(actor: CurrentUser, user: User) -> None:
    """Учётку сотрудника платформы меняет только платформа: иначе администратор клиента сбросил бы пароль
    владельцу платформы, заведённому в его клиенте, и получил бы доступ ко всем клиентам."""
    if not can_grant(actor.role, user.role):
        raise AuthorizationError("platform accounts are managed by the platform only", code="ROLE_NOT_GRANTABLE")


def contacts(email: str | None, phone: str | None) -> tuple[str | None, str | None]:
    """Нормализованные логины; хотя бы один обязателен."""
    e = normalize_email(email) if email and email.strip() else None
    p = normalize_phone(phone) if phone and phone.strip() else None
    if e is None and p is None:
        raise ValidationError("email or phone is required", code="LOGIN_REQUIRED")
    return e, p


async def ensure_logins_free(repos: IdentityRepositories, email: str | None, phone: str | None) -> None:
    if email is not None and await repos.users.lookup_by_email(email) is not None:
        raise ConflictError("email is already registered", code="EMAIL_TAKEN")
    if phone is not None and await repos.users.lookup_by_phone(phone) is not None:
        raise ConflictError("phone is already registered", code="PHONE_TAKEN")


class UserService:
    def __init__(
        self, *, uow_factory: UowFactory, repos: ReposFactory, hasher: PasswordHasher, clock: Clock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._hasher = hasher
        self._clock = clock or SystemClock()

    async def create(self, actor: CurrentUser, cmd: CreateUserCommand, *, reason: str) -> UserView:
        check_password(cmd.password)
        ensure_can_grant(actor, cmd.role)
        ensure_lots_within_actor(actor, cmd.lot_scope)
        email, phone = contacts(cmd.email, cmd.phone)
        user = User(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            email=email,
            password_hash=self._hasher.hash(cmd.password),
            role=cmd.role,
            display_name=cmd.display_name,
            locale=cmd.locale,
            lot_scope=cmd.lot_scope,
            phone=phone,
            must_change_password=cmd.temporary_password,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await ensure_logins_free(repos, email, phone)
            await repos.users.add(user)
            await repos.users.replace_lot_scope(user.id, cmd.lot_scope)
            await self._audit(repos, actor, "USER_CREATED", user, reason, {"role": cmd.role.value})
            created = await repos.users.get(user.id)
        return UserView.of(created or user)

    async def change_role(self, actor: CurrentUser, user_id: UUID, new_role: Role, *, reason: str) -> UserView:
        ensure_can_grant(actor, new_role)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            user = await repos.users.get_for_update(user_id)
            if user is None:
                raise NotFoundError("user not found")
            ensure_can_manage(actor, user)
            user.change_role(new_role)
            await repos.users.save(user)
            for event in user.pull_events():
                if isinstance(event, RoleChanged):
                    uow.publish(
                        IntegrationEvent(
                            type="identity.user.role_changed.v1",
                            tenant_id=user.tenant_id,
                            aggregate_type="user",
                            aggregate_id=user.id,
                            payload={
                                "user_id": str(user.id),
                                "old_role": event.old_role.value,
                                "new_role": event.new_role.value,
                            },
                        )
                    )
            await self._audit(repos, actor, "ROLE_CHANGED", user, reason, {"new_role": new_role.value})
        return UserView.of(user)

    async def deactivate(self, actor: CurrentUser, user_id: UUID, *, reason: str) -> None:
        if user_id == actor.user_id:
            raise ValidationError("cannot deactivate yourself")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            user = await repos.users.get_for_update(user_id)
            if user is None:
                raise NotFoundError("user not found")
            ensure_can_manage(actor, user)
            user.deactivate()
            await repos.users.save(user)
            await self._audit(repos, actor, "USER_DEACTIVATED", user, reason, {})

    # ---- v2 этап 3: самостоятельная регистрация (AccountProvisioner) ----
    def prepare_password(self, password: str) -> str:
        check_password(password)
        return self._hasher.hash(password)

    async def email_taken(self, email: str) -> bool:
        async with self._uow_factory(None) as uow:
            return await self._repos(uow).users.lookup_by_email(email.strip().lower()) is not None

    async def provision_first_admin_in(
        self, uow: UnitOfWork, tenant_id: UUID, *, email: str, password_hash: str, display_name: str, locale: str
    ) -> UserView:
        """Первый TENANT_ADMIN нового клиента в UoW регистрации: клиент, ключи и администратор появляются атомарно."""
        repos = self._repos(uow)
        email = email.strip().lower()
        await ensure_logins_free(repos, email, None)
        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=email,
            password_hash=password_hash,
            role=Role.TENANT_ADMIN,
            display_name=display_name,
            locale=locale,
        )
        await repos.users.add(user)
        await repos.audit.record(
            AuditEntry(
                tenant_id=tenant_id,
                action="USER_CREATED",
                entity_type="app_user",
                entity_id=user.id,
                actor_type=ActorType.SYSTEM,
                reason="самостоятельная регистрация клиента",
                payload={"role": Role.TENANT_ADMIN.value, "signup": True},
            )
        )
        return UserView.of(user)

    async def update(
        self,
        actor: CurrentUser,
        user_id: UUID,
        *,
        display_name: str | None,
        locale: str | None,
        lot_scope: frozenset[UUID] | None,
        active: bool | None,
        reason: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> UserView:
        """v2 этап 4: имя, логины (e-mail/телефон; пустая строка — убрать), язык, площадки, повторное включение."""
        if active is False and user_id == actor.user_id:
            raise ValidationError("cannot deactivate yourself")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            user = await repos.users.get_for_update(user_id)
            if user is None:
                raise NotFoundError("user not found")
            ensure_can_manage(actor, user)
            changed: dict[str, object] = {}
            if email is not None or phone is not None:
                new_email, new_phone = contacts(
                    user.email if email is None else email, user.phone if phone is None else phone
                )
                await ensure_logins_free(
                    repos,
                    new_email if new_email != user.email else None,
                    new_phone if new_phone != user.phone else None,
                )
                if (new_email, new_phone) != (user.email, user.phone):
                    changed["logins"] = {"email": new_email is not None, "phone": new_phone is not None}
                    user.email, user.phone = new_email, new_phone
            if display_name is not None and display_name.strip() and display_name.strip() != user.display_name:
                user.display_name = display_name.strip()
                changed["display_name"] = user.display_name
            if locale is not None and locale != user.locale:
                user.locale = locale
                changed["locale"] = locale
            if active is True and not user.active:
                user.reactivate()
                changed["active"] = True
            elif active is False and user.active:
                user.deactivate()
                changed["active"] = False
            await repos.users.save(user)
            if lot_scope is not None and lot_scope != user.lot_scope:
                ensure_lots_within_actor(actor, lot_scope)
                await repos.users.replace_lot_scope(user.id, lot_scope)
                user.lot_scope = lot_scope
                changed["lot_scope"] = sorted(str(lot) for lot in lot_scope)
            await self._audit(repos, actor, "USER_UPDATED", user, reason, changed)
        return UserView.of(user)

    async def reset_password(self, actor: CurrentUser, user_id: UUID, *, password: str, reason: str) -> None:
        """v2: временный пароль от администратора; сотрудник сменит его при входе. Все сессии сотрудника завершаются."""
        check_password(password)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            user = await repos.users.get_for_update(user_id)
            if user is None:
                raise NotFoundError("user not found")
            ensure_can_manage(actor, user)
            user.set_password_hash(self._hasher.hash(password), temporary=user_id != actor.user_id)
            await repos.users.save(user)
            revoked = await repos.refresh_tokens.revoke_user(user.id, "PASSWORD_RESET", self._clock.now())
            await self._audit(repos, actor, "PASSWORD_RESET_BY_ADMIN", user, reason, {"sessions_revoked": revoked})

    async def change_own_password(self, actor: CurrentUser, *, current_password: str, new_password: str) -> None:
        """Смена своего пароля (в том числе обязательная — после временного). Остальные сессии завершаются;
        клиент входит заново новым паролем."""
        check_password(new_password)
        if new_password == current_password:
            raise ValidationError("new password must differ", code="PASSWORD_UNCHANGED")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            user = await repos.users.get_for_update(actor.user_id)
            if user is None:
                raise NotFoundError("user not found")
            if not self._hasher.verify(user.password_hash, current_password):
                raise ValidationError("current password is wrong", code="WRONG_PASSWORD")
            user.set_password_hash(self._hasher.hash(new_password), temporary=False)
            await repos.users.save(user)
            revoked = await repos.refresh_tokens.revoke_user(user.id, "PASSWORD_CHANGED", self._clock.now())
            await self._audit(
                repos, actor, "PASSWORD_CHANGED", user, "смена своего пароля", {"sessions_revoked": revoked}
            )

    async def list(self, actor: CurrentUser, *, limit: int, after: tuple[datetime, UUID] | None) -> list[UserView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return [UserView.of(u) for u in await self._repos(uow).users.list(limit=limit, after=after)]

    async def get(self, actor: CurrentUser, user_id: UUID) -> UserView:
        async with self._uow_factory(actor.tenant_id) as uow:
            user = await self._repos(uow).users.get(user_id)
        if user is None:
            raise NotFoundError("user not found")
        return UserView.of(user)

    @staticmethod
    async def _audit(
        repos: IdentityRepositories,
        actor: CurrentUser,
        action: str,
        user: User,
        reason: str,
        payload: dict[str, object],
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="user",
                entity_id=user.id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload=payload,
            )
        )
