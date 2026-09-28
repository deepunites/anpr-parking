"""API-ключи внешних систем: выпуск (ключ показывается один раз), список, отзыв, проверка на каждом запросе."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser, Permission, Role
from parking.modules.integrations.application.ports import ApiKeyRepository
from parking.modules.integrations.domain.api_key import ApiKey, check_scopes, generate_key, split_prefix
from parking.platform.crypto import hmac_camera_key
from parking.platform.db.uow import UnitOfWork
from parking.shared.clock import Clock
from parking.shared.errors import AuthenticationError, NotFoundError, ValidationError

TOUCH_EVERY = timedelta(minutes=1)
_DUMMY = b"\x00" * 32


@dataclass(frozen=True, slots=True)
class ApiKeyRepositories:
    keys: ApiKeyRepository
    audit: AuditWriter


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    key: ApiKey
    raw: str


class ApiKeyService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], ApiKeyRepositories],
        pepper: bytes,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._pepper = pepper
        self._clock = clock
        self._touched: dict[UUID, datetime] = {}

    def _digest(self, raw: str) -> bytes:
        return hmac_camera_key(self._pepper, "api:" + raw)  # отдельный домен HMAC от ключей камер

    async def create(
        self,
        actor: CurrentUser,
        *,
        name: str,
        scopes: list[str],
        lot_scope: frozenset[UUID],
        expires_in_days: int | None,
        reason: str,
    ) -> IssuedApiKey:
        allowed = {p.value for p in actor.permissions}
        granted = check_scopes(scopes, allowed)
        own_lots = actor.allowed_lots()
        if own_lots is not None and (not lot_scope or not lot_scope <= own_lots):
            raise ValidationError("key lots must be within your own lots", code="LOTS_OUT_OF_SCOPE")
        if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
            raise ValidationError("expires_in_days must be 1..3650")
        now = self._clock.now()
        secret = generate_key()
        key = ApiKey(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            name=name.strip(),
            prefix=secret.prefix,
            scopes=granted,
            created_by=actor.user_id,
            created_at=now,
            lot_scope=lot_scope,
            expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await repos.keys.add(key, self._digest(secret.raw))
            await _audit(repos, actor, "API_KEY_CREATED", key.id, reason, {"scopes": list(granted), "name": key.name})
        return IssuedApiKey(key=key, raw=secret.raw)

    async def keys(self, actor: CurrentUser) -> list[ApiKey]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).keys.list()

    async def revoke(self, actor: CurrentUser, key_id: UUID, *, reason: str) -> None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            key = await repos.keys.get_for_update(key_id)
            if key is None:
                raise NotFoundError("api key not found")
            if key.revoked_at is None:
                await repos.keys.revoke(key_id, at=self._clock.now(), by=actor.user_id)
                await _audit(repos, actor, "API_KEY_REVOKED", key_id, reason, {"name": key.name})

    async def authenticate(self, raw: str) -> CurrentUser:
        """Принципал ключа: права — только его scopes (не роль), площадки — его lot_scope. Ответ при неверном
        и отозванном ключе одинаковый; сравнение HMAC выполняется всегда (время ответа не выдаёт префикс)."""
        prefix = split_prefix(raw)
        if prefix is None:
            raise AuthenticationError("invalid api key")
        async with self._uow_factory(None) as uow:
            found = await self._repos(uow).keys.lookup(prefix)
        ok = hmac.compare_digest(self._digest(raw), found.key_hmac if found is not None else _DUMMY)
        now = self._clock.now()
        if (
            found is None
            or not ok
            or found.revoked_at is not None
            or (found.expires_at is not None and found.expires_at <= now)
            or not found.tenant_active
            or not found.creator_active  # сотрудник уволен — его ключи перестают работать
        ):
            raise AuthenticationError("invalid api key")
        last = self._touched.get(found.id)
        if last is None or now - last > TOUCH_EVERY:  # last_used_at — не чаще раза в минуту на процесс
            self._touched[found.id] = now
            async with self._uow_factory(found.tenant_id) as uow:
                await self._repos(uow).keys.touch(found.id, now)
        # действует от имени создателя (аудит и ссылки на пользователя), права — только scopes ключа
        return CurrentUser(
            user_id=found.created_by,
            tenant_id=found.tenant_id,
            role=Role.VIEWER,
            lot_scope=found.lot_scope,
            scopes=frozenset(Permission(s) for s in found.scopes),
            api_key_name=found.name,
        )


async def _audit(
    repos: ApiKeyRepositories, actor: CurrentUser, action: str, entity_id: UUID, reason: str, payload: dict[str, object]
) -> None:
    await repos.audit.record(
        AuditEntry(
            tenant_id=actor.tenant_id,
            action=action,
            entity_type="api_key",
            entity_id=entity_id,
            actor_type=ActorType.USER,
            actor_id=actor.user_id,
            user_id=actor.user_id,
            reason=reason,
            payload=payload,
        )
    )
