"""v2 этап 3: SQL/Redis-адаптеры подписки, регистрации и списка клиентов владельца платформы."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.tenancy.application.ports import PlatformTenantRow, SignupRecord
from parking.modules.tenancy.domain.subscription import Plan, TenantStatus, TenantSubscription
from parking.platform.crypto import KeyWrapper, LayeredKeyProvider
from parking.platform.logging import get_logger

log = get_logger("tenancy.subscription")

_PLAN_COLUMNS = "code, name, max_lots, max_places, max_cameras, price_minor, trial_days, sort_order"


def _plan(r: Any) -> Plan:
    return Plan(
        code=r.code,
        name=r.name,
        max_lots=r.max_lots,
        max_places=r.max_places,
        max_cameras=r.max_cameras,
        price_minor=r.price_minor,
        trial_days=r.trial_days,
        sort_order=r.sort_order,
    )


class SqlTenantSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def plans(self) -> list[Plan]:
        rows = (
            await self._s.execute(text(f"SELECT {_PLAN_COLUMNS} FROM platform_plan WHERE active ORDER BY sort_order"))
        ).all()
        return [_plan(r) for r in rows]

    async def plan(self, code: str) -> Plan | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_PLAN_COLUMNS} FROM platform_plan WHERE code = :code AND active"), {"code": code}
            )
        ).first()
        return None if row is None else _plan(row)

    async def get(self, tenant_id: UUID) -> TenantSubscription | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT t.id, t.status, t.trial_ends_at, t.paid_until, "
                    "p.code, p.name, p.max_lots, p.max_places, p.max_cameras, p.price_minor, p.trial_days, p.sort_order "
                    "FROM tenant t JOIN platform_plan p ON p.code = t.plan_code WHERE t.id = :id"
                ),
                {"id": tenant_id},
            )
        ).first()
        if row is None:
            return None
        return TenantSubscription(
            tenant_id=row.id,
            plan=_plan(row),
            status=TenantStatus(row.status),
            trial_ends_at=row.trial_ends_at,
            paid_until=row.paid_until,
        )

    async def set_plan(self, tenant_id: UUID, plan_code: str) -> None:
        await self._s.execute(
            text("UPDATE tenant SET plan_code = :p WHERE id = :id"), {"p": plan_code, "id": tenant_id}
        )

    async def admin_update(
        self,
        tenant_id: UUID,
        *,
        status: TenantStatus | None,
        plan_code: str | None,
        paid_until: datetime | None,
        trial_ends_at: datetime | None,
    ) -> None:
        await self._s.execute(
            text(
                "UPDATE tenant SET status = COALESCE(:status, status), plan_code = COALESCE(:plan, plan_code), "
                "paid_until = COALESCE(:paid, paid_until), trial_ends_at = COALESCE(:trial, trial_ends_at) "
                "WHERE id = :id"
            ),
            {
                "id": tenant_id,
                "status": status.value if status else None,
                "plan": plan_code,
                "paid": paid_until,
                "trial": trial_ends_at,
            },
        )

    async def create_tenant(
        self,
        *,
        tenant_id: UUID,
        code: str,
        name: str,
        plan_code: str,
        trial_ends_at: datetime,
        contact_phone: str | None,
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO tenant (id, code, name, crypto_key_ref, plan_code, status, trial_ends_at, contact_phone, "
                "signup_source) VALUES (:id, :code, :name, 'db:tenant_key', :plan, 'TRIAL', :trial, :phone, 'self')"
            ),
            {
                "id": tenant_id,
                "code": code,
                "name": name,
                "plan": plan_code,
                "trial": trial_ends_at,
                "phone": contact_phone,
            },
        )

    async def add_wrapped_keys(
        self, tenant_id: UUID, version: int, wrapped_hmac: bytes, wrapped_dek: bytes, kek_id: str
    ) -> None:
        await self._s.execute(
            text(
                "INSERT INTO tenant_key (tenant_id, version, wrapped_hmac, wrapped_dek, kek_id) "
                "VALUES (:t, :v, :h, :d, :k)"
            ),
            {"t": tenant_id, "v": version, "h": wrapped_hmac, "d": wrapped_dek, "k": kek_id},
        )

    async def usage(self, tenant_id: UUID) -> tuple[int, int]:
        row = (
            await self._s.execute(
                text(
                    "SELECT count(*) AS lots, coalesce(sum(capacity), 0) AS places FROM parking_lot "
                    "WHERE tenant_id = :t AND active"
                ),
                {"t": tenant_id},
            )
        ).one()
        return int(row.lots), int(row.places)


class RedisSignupStore:
    """Заявки на регистрацию живут 15 минут в Redis: в БД нет строк без клиента и не нужен RLS для «ничьих» данных."""

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    @staticmethod
    def _key(signup_id: str) -> str:
        return f"signup:{signup_id}"

    async def save(self, record: SignupRecord, ttl_seconds: int) -> None:
        payload = {
            "company": record.company,
            "name": record.name,
            "email": record.email,
            "phone": record.phone,
            "password_hash": record.password_hash,
            "plan_code": record.plan_code,
            "locale": record.locale,
            "code_hash": record.code_hash,
            "expires_at": record.expires_at.isoformat(),
        }
        await self._r.set(self._key(record.signup_id), json.dumps(payload), ex=ttl_seconds)
        await self._r.set(self._key(record.signup_id) + ":attempts", 0, ex=ttl_seconds)

    async def get(self, signup_id: str) -> SignupRecord | None:
        raw = await self._r.get(self._key(signup_id))
        if raw is None:
            return None
        data = json.loads(raw)
        attempts = int(await self._r.get(self._key(signup_id) + ":attempts") or 0)
        return SignupRecord(
            signup_id=signup_id,
            company=data["company"],
            name=data["name"],
            email=data["email"],
            phone=data["phone"],
            password_hash=data["password_hash"],
            plan_code=data["plan_code"],
            locale=data["locale"],
            code_hash=data["code_hash"],
            attempts=attempts,
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )

    async def bump_attempts(self, signup_id: str) -> int:
        return int(await self._r.incr(self._key(signup_id) + ":attempts"))

    async def delete(self, signup_id: str) -> None:
        await self._r.delete(self._key(signup_id), self._key(signup_id) + ":attempts")

    async def hit_ip(self, ip: str, window_seconds: int) -> int:
        key = f"signup:ip:{ip}"
        count = int(await self._r.incr(key))
        if count == 1:
            await self._r.expire(key, window_seconds)
        return count


class ConsoleCodeSender:
    """Стенд без SMS/Telegram (A-76): код пишется в журнал процесса только в dev. В staging/prod этот отправитель
    не используется — там регистрация выключена до подключения реального канала."""

    channel = "console"

    def __init__(self, *, log_codes: bool) -> None:
        self._log_codes = log_codes

    async def send(self, *, phone: str | None, email: str, code: str) -> None:
        if self._log_codes:
            log.info("signup_code", code=code, email_domain=email.rsplit("@", maxsplit=1)[-1])
        else:
            log.info("signup_code_issued", email_domain=email.rsplit("@", maxsplit=1)[-1])


class SqlPlatformDirectory:
    """Список клиентов для владельца платформы. Роль maintenance (кросс-tenant чтение, A-14: только агрегаты)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def list_tenants(self) -> list[PlatformTenantRow]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT t.id, t.code, t.name, t.plan_code, t.status, t.trial_ends_at, t.paid_until, t.created_at,
                               t.contact_phone,
                               (SELECT count(*) FROM parking_lot l WHERE l.tenant_id = t.id AND l.active) AS lots,
                               (SELECT coalesce(sum(capacity), 0) FROM parking_lot l WHERE l.tenant_id = t.id AND l.active) AS places,
                               (SELECT count(*) FROM camera c WHERE c.tenant_id = t.id AND c.active) AS cameras,
                               (SELECT count(*) FROM app_user u WHERE u.tenant_id = t.id AND u.active) AS users,
                               (SELECT count(*) FROM parking_session ps WHERE ps.tenant_id = t.id
                                  AND ps.entry_time > now() - interval '30 days') AS sessions_30d
                        FROM tenant t ORDER BY t.created_at DESC
                        """
                    )
                )
            ).all()
        return [
            PlatformTenantRow(
                id=r.id,
                code=r.code,
                name=r.name,
                plan_code=r.plan_code,
                status=r.status,
                trial_ends_at=r.trial_ends_at,
                paid_until=r.paid_until,
                created_at=r.created_at,
                contact_phone=r.contact_phone,
                lots=int(r.lots),
                places=int(r.places),
                cameras=int(r.cameras),
                users=int(r.users),
                sessions_30d=int(r.sessions_30d),
            )
            for r in rows
        ]


async def refresh_wrapped_keys(
    session_factory: async_sessionmaker[AsyncSession], wrapper: KeyWrapper, provider: LayeredKeyProvider
) -> int:
    """Подхват ключей клиентов, зарегистрированных в других процессах (maintenance: кросс-tenant чтение)."""
    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT ON (tenant_id) tenant_id, version, wrapped_hmac, wrapped_dek FROM tenant_key "
                    "ORDER BY tenant_id, version DESC"
                )
            )
        ).all()
    loaded = 0
    for r in rows:
        try:
            provider.add(
                r.tenant_id, wrapper.unwrap(r.tenant_id, r.version, bytes(r.wrapped_hmac), bytes(r.wrapped_dek))
            )
            loaded += 1
        except Exception as exc:  # чужой KEK / повреждение — клиент недоступен, остальные работают
            log.error("tenant_key_unwrap_failed", tenant_id=str(r.tenant_id), error_type=type(exc).__name__)
    return loaded
