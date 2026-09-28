"""v2 этап 3 «Продажа»: подписка клиента (тариф, лимиты, пробный период), самостоятельная регистрация, управление
клиентами владельцем платформы. Подписка ограничивает только настройку, не проезд машин (A-82)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import AccountProvisioner, CurrentUser
from parking.modules.tenancy.application.ports import (
    CodeSender,
    PlatformDirectory,
    PlatformTenantRow,
    SignupRecord,
    SignupStore,
    SubscriptionRepository,
)
from parking.modules.tenancy.domain.subscription import Plan, Resource, TenantStatus, TenantSubscription
from parking.platform.crypto import KeyWrapper, LayeredKeyProvider, TenantKeys
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import (
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationError,
)

log = get_logger("tenancy.subscription")

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
_PHONE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
MAX_CODE_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class SubscriptionRepositories:
    subscriptions: SubscriptionRepository
    audit: AuditWriter


@dataclass(frozen=True, slots=True)
class Usage:
    lots: int
    places: int
    cameras: int


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    subscription: TenantSubscription
    usage: Usage
    plans: list[Plan]
    blocked_reason: str | None
    trial_days_left: int | None


@dataclass(frozen=True, slots=True)
class SubscriptionBrief:
    plan_code: str
    status: str
    trial_ends_at: datetime | None
    paid_until: datetime | None
    blocked_reason: str | None
    trial_days_left: int | None


@dataclass(frozen=True, slots=True)
class SignupStarted:
    signup_id: str
    channel: str
    expires_at: datetime
    dev_code: str | None  # только dev/test (settings.signup_dev_codes)


@dataclass(frozen=True, slots=True)
class SignupCompleted:
    tenant_id: UUID
    email: str


class SubscriptionService:
    """Лимиты тарифа и экран «Подписка» клиента. Реализует SubscriptionGate для других модулей."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], SubscriptionRepositories],
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._clock = clock

    async def _load(self, tenant_id: UUID) -> TenantSubscription:
        async with self._uow_factory(tenant_id) as uow:
            sub = await self._repos(uow).subscriptions.get(tenant_id)
        if sub is None:
            raise NotFoundError("tenant not found")
        return sub

    async def ensure(self, tenant_id: UUID, resource: str, used_after: int) -> None:
        sub = await self._load(tenant_id)
        sub.ensure_within(Resource(resource), used_after, self._clock.now())

    async def ensure_can_configure(self, tenant_id: UUID) -> None:
        (await self._load(tenant_id)).ensure_can_configure(self._clock.now())

    async def usage(self, tenant_id: UUID) -> tuple[int, int]:
        async with self._uow_factory(tenant_id) as uow:
            return await self._repos(uow).subscriptions.usage(tenant_id)

    async def plans(self) -> list[Plan]:
        async with self._uow_factory(None) as uow:
            return await self._repos(uow).subscriptions.plans()

    async def view(self, actor: CurrentUser, cameras: int) -> SubscriptionView:
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            sub = await repos.subscriptions.get(actor.tenant_id)
            lots, places = await repos.subscriptions.usage(actor.tenant_id)
            plans = await repos.subscriptions.plans()
        if sub is None:
            raise NotFoundError("tenant not found")
        return SubscriptionView(
            subscription=sub,
            usage=Usage(lots=lots, places=places, cameras=cameras),
            plans=plans,
            blocked_reason=sub.blocked_reason(now),
            trial_days_left=_trial_days_left(sub, now),
        )

    async def brief(self, tenant_id: UUID) -> SubscriptionBrief | None:
        """Для /auth/me: баннер пробного периода и блокировки в шапке панели (без usage — дёшево)."""
        now = self._clock.now()
        async with self._uow_factory(tenant_id) as uow:
            sub = await self._repos(uow).subscriptions.get(tenant_id)
        if sub is None:
            return None
        return SubscriptionBrief(
            plan_code=sub.plan.code,
            status=sub.status.value,
            trial_ends_at=sub.trial_ends_at,
            paid_until=sub.paid_until,
            blocked_reason=sub.blocked_reason(now),
            trial_days_left=_trial_days_left(sub, now),
        )

    async def change_plan(self, actor: CurrentUser, plan_code: str, *, cameras: int, reason: str) -> None:
        """Клиент выбирает тариф сам; на меньший — только если текущее использование в него помещается."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            plan = await repos.subscriptions.plan(plan_code)
            if plan is None:
                raise ValidationError("unknown plan", code="UNKNOWN_PLAN")
            lots, places = await repos.subscriptions.usage(actor.tenant_id)
            for resource, used in ((Resource.LOTS, lots), (Resource.PLACES, places), (Resource.CAMERAS, cameras)):
                limit = plan.limit(resource)
                if limit is not None and used > limit:
                    raise ConflictError(
                        "current usage does not fit the plan",
                        code="PLAN_TOO_SMALL",
                        details={"resource": resource.value, "limit": limit, "used": used, "plan": plan.code},
                    )
            await repos.subscriptions.set_plan(actor.tenant_id, plan.code)
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="PLAN_CHANGED",
                    entity_type="tenant",
                    entity_id=actor.tenant_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"plan": plan.code},
                )
            )


class SignupService:
    """Самостоятельная регистрация: заявка с кодом подтверждения (15 мин, 5 попыток) → клиент в пробном периоде,
    ключи шифрования номеров, первый администратор — одной транзакцией. Пароль в заявке хранится только как argon2id."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], SubscriptionRepositories],
        store: SignupStore,
        sender: CodeSender,
        accounts: AccountProvisioner,
        wrapper: KeyWrapper | None,
        keys: LayeredKeyProvider,
        clock: Clock,
        code_ttl_seconds: int,
        rate_per_hour: int,
        dev_codes: bool,
        enabled: bool,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._store = store
        self._sender = sender
        self._accounts = accounts
        self._wrapper = wrapper
        self._keys = keys
        self._clock = clock
        self._ttl = code_ttl_seconds
        self._rate = rate_per_hour
        self._dev_codes = dev_codes
        self._enabled = enabled

    def _check_enabled(self) -> KeyWrapper:
        if not self._enabled or self._wrapper is None:
            raise ServiceUnavailableError("self-service signup is not configured", code="SIGNUP_DISABLED")
        return self._wrapper

    async def start(
        self,
        *,
        company: str,
        name: str,
        email: str,
        phone: str | None,
        password: str,
        plan_code: str,
        locale: str,
        ip: str | None,
    ) -> SignupStarted:
        self._check_enabled()
        if ip is not None and await self._store.hit_ip(ip, 3600) > self._rate:
            raise RateLimitedError("too many signup attempts from this address", retry_after_seconds=3600)
        company, name, email = " ".join(company.split()), " ".join(name.split()), email.strip().lower()
        if len(company) < 2 or len(name) < 2:
            raise ValidationError("company and name are required")
        if not _EMAIL.match(email):
            raise ValidationError("invalid email", code="INVALID_EMAIL")
        if phone is not None and phone.strip() and not _PHONE.match(phone.strip()):
            raise ValidationError("invalid phone", code="INVALID_PHONE")
        async with self._uow_factory(None) as uow:
            plan = await self._repos(uow).subscriptions.plan(plan_code)
        if plan is None:
            raise ValidationError("unknown plan", code="UNKNOWN_PLAN")
        if await self._accounts.email_taken(email):
            raise ConflictError("email is already registered", code="EMAIL_TAKEN")
        password_hash = self._accounts.prepare_password(password)
        signup_id = secrets.token_urlsafe(18)
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = self._clock.now() + timedelta(seconds=self._ttl)
        await self._store.save(
            SignupRecord(
                signup_id=signup_id,
                company=company[:150],
                name=name[:120],
                email=email,
                phone=phone.strip() if phone and phone.strip() else None,
                password_hash=password_hash,
                plan_code=plan.code,
                locale=locale if locale in {"ru", "uz"} else "ru",
                code_hash=_code_hash(signup_id, code),
                attempts=0,
                expires_at=expires_at,
            ),
            self._ttl,
        )
        try:
            await self._sender.send(phone=phone, email=email, code=code)
        except (
            Exception
        ) as exc:  # v2 этап 8: канал кодов (SMS/Telegram) недоступен — понятный 503, RUNBOOK sms-gateway-down
            log.error("signup_code_send_failed", channel=self._sender.channel, error_type=type(exc).__name__)
            raise ServiceUnavailableError("verification code could not be sent, try later") from exc
        log.info("signup_started", plan=plan.code, channel=self._sender.channel)
        return SignupStarted(
            signup_id=signup_id,
            channel=self._sender.channel,
            expires_at=expires_at,
            dev_code=code if self._dev_codes else None,
        )

    async def verify(self, signup_id: str, code: str) -> SignupCompleted:
        wrapper = self._check_enabled()
        record = await self._store.get(signup_id)
        if record is None:
            raise NotFoundError("signup request not found or expired", code="SIGNUP_EXPIRED")
        if record.attempts >= MAX_CODE_ATTEMPTS:
            await self._store.delete(signup_id)
            raise RateLimitedError("too many wrong codes, start again", retry_after_seconds=60)
        if not hmac.compare_digest(record.code_hash, _code_hash(signup_id, code.strip())):
            left = MAX_CODE_ATTEMPTS - await self._store.bump_attempts(signup_id)
            raise ValidationError("wrong confirmation code", code="WRONG_CODE", details={"attempts_left": max(0, left)})
        now = self._clock.now()
        tenant_id = uuid4()
        keys = KeyWrapper.generate()
        wrapped_hmac, wrapped_dek = wrapper.wrap(tenant_id, keys)
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            plan = await repos.subscriptions.plan(record.plan_code)
            if plan is None:
                raise ValidationError("unknown plan", code="UNKNOWN_PLAN")
            await repos.subscriptions.create_tenant(
                tenant_id=tenant_id,
                code=f"c-{tenant_id.hex[:10]}",
                name=record.company,
                plan_code=plan.code,
                trial_ends_at=now + timedelta(days=plan.trial_days),
                contact_phone=record.phone,
            )
            await repos.subscriptions.add_wrapped_keys(
                tenant_id, keys.version, wrapped_hmac, wrapped_dek, wrapper.kek_id
            )
            await self._accounts.provision_first_admin_in(
                uow,
                tenant_id,
                email=record.email,
                password_hash=record.password_hash,
                display_name=record.name,
                locale=record.locale,
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    action="TENANT_SIGNED_UP",
                    entity_type="tenant",
                    entity_id=tenant_id,
                    actor_type=ActorType.SYSTEM,
                    reason="самостоятельная регистрация",
                    payload={"plan": plan.code, "trial_days": plan.trial_days},
                )
            )
            uow.after_commit(_add_keys(self._keys, tenant_id, keys))
        await self._store.delete(signup_id)
        log.info("tenant_signed_up", tenant_id=str(tenant_id), plan=record.plan_code)
        return SignupCompleted(tenant_id=tenant_id, email=record.email)


class PlatformService:
    """Владелец платформы: список клиентов (агрегаты) и смена статуса/тарифа/оплаты. Данных водителей здесь нет."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], SubscriptionRepositories],
        directory: PlatformDirectory,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._directory = directory

    async def list_tenants(self) -> list[PlatformTenantRow]:
        return await self._directory.list_tenants()

    async def update_tenant(
        self,
        actor: CurrentUser,
        tenant_id: UUID,
        *,
        status: TenantStatus | None,
        plan_code: str | None,
        paid_until: datetime | None,
        trial_ends_at: datetime | None,
        reason: str,
    ) -> None:
        # контекст — целевой клиент: RLS пропускает UPDATE только этой строки tenant, аудит пишется в его журнал
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.subscriptions.get(tenant_id) is None:
                raise NotFoundError("tenant not found")
            if plan_code is not None and await repos.subscriptions.plan(plan_code) is None:
                raise ValidationError("unknown plan", code="UNKNOWN_PLAN")
            await repos.subscriptions.admin_update(
                tenant_id, status=status, plan_code=plan_code, paid_until=paid_until, trial_ends_at=trial_ends_at
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    action="TENANT_SUBSCRIPTION_UPDATED",
                    entity_type="tenant",
                    entity_id=tenant_id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,  # сотрудник платформы: его учётка в клиенте платформы (actor_tenant)
                    reason=reason,
                    payload={
                        "actor_tenant": str(actor.tenant_id),
                        "status": status.value if status else None,
                        "plan": plan_code,
                        "paid_until": paid_until.isoformat() if paid_until else None,
                        "trial_ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
                    },
                )
            )


def _trial_days_left(sub: TenantSubscription, now: datetime) -> int | None:
    if sub.status is not TenantStatus.TRIAL or sub.trial_ends_at is None:
        return None
    left = sub.trial_ends_at - now
    return max(0, left.days + (1 if left.seconds > 0 else 0))


def _code_hash(signup_id: str, code: str) -> str:
    return hashlib.sha256(f"{signup_id}:{code}".encode()).hexdigest()


def _add_keys(provider: LayeredKeyProvider, tenant_id: UUID, keys: TenantKeys) -> Callable[[], Awaitable[None]]:
    """После коммита: ключи нового клиента сразу доступны в этом процессе; другие процессы подхватят refresh'ем."""

    async def hook() -> None:
        provider.add(tenant_id, keys)

    return hook
