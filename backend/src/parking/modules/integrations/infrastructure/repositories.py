"""SQL-адаптеры интеграций: ключи, подписки и доставки вебхуков, очередь диспетчера (maintenance), журнал."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.integrations.application.ports import ApiKeyLookup, DueDelivery
from parking.modules.integrations.domain.api_key import ApiKey
from parking.modules.integrations.domain.journal import JournalEntry
from parking.modules.integrations.domain.webhook import AUTO_DISABLE_AFTER, Delivery, WebhookEndpoint

log = structlog.get_logger(__name__)


def _json(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value)


_KEY_COLUMNS = (
    "id, tenant_id, name, prefix, scopes, lot_scope, created_by, created_at, expires_at, last_used_at, revoked_at"
)


def _key(r: Any) -> ApiKey:
    return ApiKey(
        id=r.id,
        tenant_id=r.tenant_id,
        name=r.name,
        prefix=r.prefix,
        scopes=tuple(r.scopes),
        created_by=r.created_by,
        created_at=r.created_at,
        lot_scope=frozenset(r.lot_scope or ()),
        expires_at=r.expires_at,
        last_used_at=r.last_used_at,
        revoked_at=r.revoked_at,
    )


class SqlApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, key: ApiKey, key_hmac: bytes) -> None:
        await self._s.execute(
            text(
                "INSERT INTO api_key (id, tenant_id, name, prefix, key_hmac, scopes, lot_scope, expires_at, created_by, "
                "created_at) VALUES (:id, :t, :name, :prefix, :hmac, :scopes, CAST(:lots AS uuid[]), :exp, :by, :at)"
            ),
            {
                "id": key.id,
                "t": key.tenant_id,
                "name": key.name,
                "prefix": key.prefix,
                "hmac": key_hmac,
                "scopes": list(key.scopes),
                "lots": [str(lot) for lot in key.lot_scope],
                "exp": key.expires_at,
                "by": key.created_by,
                "at": key.created_at,
            },
        )

    async def list(self) -> list[ApiKey]:
        rows = await self._s.execute(text(f"SELECT {_KEY_COLUMNS} FROM api_key ORDER BY created_at DESC"))
        return [_key(r) for r in rows]

    async def get_for_update(self, key_id: UUID) -> ApiKey | None:
        row = (
            await self._s.execute(text(f"SELECT {_KEY_COLUMNS} FROM api_key WHERE id = :id FOR UPDATE"), {"id": key_id})
        ).first()
        return None if row is None else _key(row)

    async def revoke(self, key_id: UUID, *, at: datetime, by: UUID) -> None:
        await self._s.execute(
            text("UPDATE api_key SET revoked_at = :at, revoked_by = :by WHERE id = :id AND revoked_at IS NULL"),
            {"id": key_id, "at": at, "by": by},
        )

    async def lookup(self, prefix: str) -> ApiKeyLookup | None:
        row = (await self._s.execute(text("SELECT * FROM app.lookup_api_key(:p)"), {"p": prefix})).first()
        if row is None:
            return None
        return ApiKeyLookup(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            key_hmac=bytes(row.key_hmac),
            scopes=tuple(row.scopes),
            lot_scope=frozenset(row.lot_scope or ()),
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            tenant_active=row.tenant_active,
            created_by=row.created_by,
            creator_active=row.creator_active,
        )

    async def touch(self, key_id: UUID, at: datetime) -> None:
        await self._s.execute(text("UPDATE api_key SET last_used_at = :at WHERE id = :id"), {"id": key_id, "at": at})


_EP_COLUMNS = (
    "id, tenant_id, url, description, events, secret_hint, active, consecutive_failures, created_by, created_at, "
    "disabled_reason"
)
_DL_COLUMNS = (
    "id, tenant_id, endpoint_id, event_type, event_id, payload, status, attempts, next_attempt_at, last_status, "
    "last_error, last_latency_ms, created_at, delivered_at"
)
_DL_COLUMNS_D = (
    "d.id, d.tenant_id, d.endpoint_id, d.event_type, d.event_id, d.payload, d.status, d.attempts, "
    "d.next_attempt_at, d.last_status, d.last_error, d.last_latency_ms, d.created_at, d.delivered_at"
)


def _endpoint(r: Any) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=r.id,
        tenant_id=r.tenant_id,
        url=r.url,
        events=tuple(r.events),
        secret_hint=r.secret_hint,
        active=r.active,
        created_by=r.created_by,
        created_at=r.created_at,
        description=r.description,
        consecutive_failures=r.consecutive_failures,
        disabled_reason=r.disabled_reason,
    )


def _delivery(r: Any) -> Delivery:
    return Delivery(
        id=r.id,
        tenant_id=r.tenant_id,
        endpoint_id=r.endpoint_id,
        event_type=r.event_type,
        event_id=r.event_id,
        payload=_json(r.payload),
        status=r.status,
        attempts=r.attempts,
        created_at=r.created_at,
        next_attempt_at=r.next_attempt_at,
        last_status=r.last_status,
        last_error=r.last_error,
        last_latency_ms=r.last_latency_ms,
        delivered_at=r.delivered_at,
    )


class SqlWebhookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_endpoint(self, endpoint: WebhookEndpoint, secret_encrypted: bytes) -> None:
        await self._s.execute(
            text(
                "INSERT INTO webhook_endpoint (id, tenant_id, url, description, events, secret_encrypted, secret_hint, "
                "active, created_by, created_at) VALUES (:id, :t, :url, :d, :ev, :sec, :hint, :active, :by, :at)"
            ),
            {
                "id": endpoint.id,
                "t": endpoint.tenant_id,
                "url": endpoint.url,
                "d": endpoint.description,
                "ev": list(endpoint.events),
                "sec": secret_encrypted,
                "hint": endpoint.secret_hint,
                "active": endpoint.active,
                "by": endpoint.created_by,
                "at": endpoint.created_at,
            },
        )

    async def endpoints(self) -> list[WebhookEndpoint]:
        rows = await self._s.execute(text(f"SELECT {_EP_COLUMNS} FROM webhook_endpoint ORDER BY created_at"))
        return [_endpoint(r) for r in rows]

    async def endpoint(self, endpoint_id: UUID) -> WebhookEndpoint | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_EP_COLUMNS} FROM webhook_endpoint WHERE id = :id"), {"id": endpoint_id}
            )
        ).first()
        return None if row is None else _endpoint(row)

    async def save_endpoint(self, endpoint: WebhookEndpoint) -> None:
        await self._s.execute(
            text(
                "UPDATE webhook_endpoint SET url = :url, description = :d, events = :ev, active = :active, "
                "consecutive_failures = :f, updated_at = now(), "
                "failing_since = CASE WHEN :active AND NOT active THEN NULL ELSE failing_since END, "
                "disabled_reason = CASE WHEN :active THEN NULL ELSE disabled_reason END WHERE id = :id"
            ),
            {
                "id": endpoint.id,
                "url": endpoint.url,
                "d": endpoint.description,
                "ev": list(endpoint.events),
                "active": endpoint.active,
                "f": endpoint.consecutive_failures,
            },
        )

    async def set_secret(self, endpoint_id: UUID, secret_encrypted: bytes, hint: str) -> None:
        await self._s.execute(
            text(
                "UPDATE webhook_endpoint SET secret_encrypted = :s, secret_hint = :h, updated_at = now() WHERE id = :id"
            ),
            {"id": endpoint_id, "s": secret_encrypted, "h": hint},
        )

    async def secret(self, endpoint_id: UUID) -> bytes | None:
        row = (
            await self._s.execute(
                text("SELECT secret_encrypted FROM webhook_endpoint WHERE id = :id"), {"id": endpoint_id}
            )
        ).first()
        return None if row is None else bytes(row.secret_encrypted)

    async def delete_endpoint(self, endpoint_id: UUID) -> None:
        await self._s.execute(text("DELETE FROM webhook_endpoint WHERE id = :id"), {"id": endpoint_id})

    async def subscribed(self, public_event: str) -> list[WebhookEndpoint]:
        rows = await self._s.execute(
            text(f"SELECT {_EP_COLUMNS} FROM webhook_endpoint WHERE active AND :ev = ANY(events)"),
            {"ev": public_event},
        )
        return [_endpoint(r) for r in rows]

    async def add_delivery(self, delivery: Delivery) -> bool:
        result = await self._s.execute(
            text(
                "INSERT INTO webhook_delivery (id, tenant_id, endpoint_id, event_type, event_id, payload, status, "
                "attempts, next_attempt_at, created_at) VALUES (:id, :t, :ep, :type, :eid, CAST(:payload AS jsonb), "
                ":status, :attempts, :next, :at) ON CONFLICT (endpoint_id, event_id) DO NOTHING"
            ),
            {
                "id": delivery.id,
                "t": delivery.tenant_id,
                "ep": delivery.endpoint_id,
                "type": delivery.event_type,
                "eid": delivery.event_id,
                "payload": json.dumps(delivery.payload, ensure_ascii=False),
                "status": delivery.status,
                "attempts": delivery.attempts,
                "next": delivery.next_attempt_at,
                "at": delivery.created_at,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def deliveries(self, *, endpoint_id: UUID | None, status: str | None, limit: int) -> list[Delivery]:
        rows = await self._s.execute(
            text(
                f"SELECT {_DL_COLUMNS} FROM webhook_delivery "
                "WHERE (CAST(:ep AS uuid) IS NULL OR endpoint_id = CAST(:ep AS uuid)) "
                "AND (CAST(:status AS text) IS NULL OR status = CAST(:status AS text)) "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"ep": endpoint_id, "status": status, "limit": limit},
        )
        return [_delivery(r) for r in rows]

    async def delivery(self, delivery_id: UUID) -> Delivery | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_DL_COLUMNS} FROM webhook_delivery WHERE id = :id"), {"id": delivery_id}
            )
        ).first()
        return None if row is None else _delivery(row)

    async def requeue(self, delivery_id: UUID, at: datetime) -> None:
        await self._s.execute(
            text(
                "UPDATE webhook_delivery SET status = 'PENDING', attempts = 0, next_attempt_at = :at, "
                "last_error = NULL WHERE id = :id"
            ),
            {"id": delivery_id, "at": at},
        )

    async def stats(self) -> dict[UUID, dict[str, Any]]:
        rows = await self._s.execute(
            text(
                """
                SELECT endpoint_id,
                       count(*) FILTER (WHERE status = 'DELIVERED' AND created_at > now() - interval '24 hours') AS ok_24h,
                       count(*) FILTER (WHERE status IN ('RETRY', 'DEAD') AND created_at > now() - interval '24 hours')
                         AS failed_24h,
                       count(*) FILTER (WHERE status = 'DEAD') AS dead,
                       max(created_at) AS last_at
                FROM webhook_delivery GROUP BY endpoint_id
                """
            )
        )
        return {
            r.endpoint_id: {"ok_24h": r.ok_24h, "failed_24h": r.failed_24h, "dead": r.dead, "last_at": r.last_at}
            for r in rows
        }


class SqlDeliveryQueue:
    """Диспетчер: роль maintenance (все клиенты), SKIP LOCKED + аренда next_attempt_at — параллельные диспетчеры
    не отправят одну доставку дважды; HTTP — между транзакциями (I-9)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def claim(self, now: datetime, lease_until: datetime, limit: int) -> list[DueDelivery]:
        async with self._factory() as s, s.begin():
            rows = (
                await s.execute(
                    text(
                        f"""
                        SELECT {_DL_COLUMNS_D}, e.url, e.secret_encrypted, e.active
                        FROM webhook_delivery d JOIN webhook_endpoint e ON e.id = d.endpoint_id
                        WHERE d.status IN ('PENDING', 'RETRY') AND d.next_attempt_at <= :now
                        ORDER BY d.next_attempt_at LIMIT :limit
                        FOR UPDATE OF d SKIP LOCKED
                        """
                    ),
                    {"now": now, "limit": limit},
                )
            ).all()
            due = [r for r in rows if r.active]
            if rows:
                await s.execute(
                    text("UPDATE webhook_delivery SET next_attempt_at = :lease WHERE id = ANY(:ids)"),
                    {"lease": lease_until, "ids": [r.id for r in rows if r.active]},
                )
                paused = [r.id for r in rows if not r.active]
                if paused:  # подписка выключена — доставки ждут включения (повтор вручную из журнала)
                    await s.execute(
                        text(
                            "UPDATE webhook_delivery SET status = 'DEAD', last_error = 'webhook disabled' "
                            "WHERE id = ANY(:ids)"
                        ),
                        {"ids": paused},
                    )
        return [DueDelivery(_delivery(r), r.url, bytes(r.secret_encrypted)) for r in due]

    async def record(
        self,
        delivery_id: UUID,
        *,
        status: str,
        attempts: int,
        next_attempt_at: datetime | None,
        http_status: int | None,
        error: str | None,
        latency_ms: int | None,
        delivered_at: datetime | None,
    ) -> None:
        async with self._factory() as s, s.begin():
            row = (
                await s.execute(
                    text(
                        "UPDATE webhook_delivery SET status = :status, attempts = :attempts, next_attempt_at = :next, "
                        "last_status = :http, last_error = :err, last_latency_ms = :lat, delivered_at = :done "
                        "WHERE id = :id RETURNING endpoint_id"
                    ),
                    {
                        "id": delivery_id,
                        "status": status,
                        "attempts": attempts,
                        "next": next_attempt_at,
                        "http": http_status,
                        "err": (error or "")[:300] or None,
                        "lat": latency_ms,
                        "done": delivered_at,
                    },
                )
            ).first()
            if row is not None:
                ep = (
                    await s.execute(
                        text(
                            """
                            UPDATE webhook_endpoint SET
                              consecutive_failures = CASE WHEN :ok THEN 0 ELSE consecutive_failures + 1 END,
                              failing_since = CASE WHEN :ok THEN NULL ELSE coalesce(failing_since, now()) END,
                              active = CASE WHEN NOT :ok AND failing_since < now() - CAST(:after AS interval)
                                            THEN false ELSE active END,
                              disabled_reason = CASE WHEN NOT :ok AND active
                                                     AND failing_since < now() - CAST(:after AS interval)
                                                     THEN 'AUTO_FAILING_3D' ELSE disabled_reason END
                            WHERE id = :ep
                            RETURNING tenant_id, active, disabled_reason
                            """
                        ),
                        {"ok": status == "DELIVERED", "ep": row.endpoint_id, "after": AUTO_DISABLE_AFTER},
                    )
                ).first()
                if ep is not None and not ep.active and ep.disabled_reason == "AUTO_FAILING_3D":
                    log.warning(
                        "webhook_endpoint_auto_disabled", endpoint_id=str(row.endpoint_id), tenant_id=str(ep.tenant_id)
                    )


_J_COLUMNS = (
    "tenant_id, lot_id, camera_id, source, received_at, outcome, status, http_status, event_type, plate_masked, "
    "message, correlation_id, latency_ms"
)


class SqlJournalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_many(self, entries: list[JournalEntry]) -> None:
        await self._s.execute(
            text(
                f"INSERT INTO event_journal ({_J_COLUMNS}) VALUES (:tenant_id, :lot_id, :camera_id, :source, "
                ":received_at, :outcome, :status, :http_status, :event_type, :plate_masked, :message, "
                ":correlation_id, :latency_ms)"
            ),
            [{k: getattr(e, k) for k in _J_COLUMNS.split(", ")} for e in entries],
        )

    async def search(
        self,
        *,
        lot_ids: frozenset[UUID] | None,
        lot_id: UUID | None,
        outcome: str | None,
        correlation_id: UUID | None,
        camera_id: UUID | None,
        since: datetime,
        limit: int,
    ) -> list[JournalEntry]:
        rows = await self._s.execute(
            text(
                f"SELECT {_J_COLUMNS} FROM event_journal WHERE received_at > :since "
                "AND (CAST(:lot_id AS uuid) IS NULL OR lot_id = CAST(:lot_id AS uuid)) "
                "AND (CAST(:outcome AS text) IS NULL OR outcome = CAST(:outcome AS text)) "
                "AND (CAST(:correlation_id AS uuid) IS NULL OR correlation_id = CAST(:correlation_id AS uuid)) "
                "AND (CAST(:camera_id AS uuid) IS NULL OR camera_id = CAST(:camera_id AS uuid)) "
                "AND (CAST(:lot_ids AS uuid[]) IS NULL OR lot_id = ANY(CAST(:lot_ids AS uuid[]))) "
                "ORDER BY received_at DESC LIMIT :limit"
            ),
            {
                "since": since,
                "limit": limit,
                "lot_id": lot_id,
                "outcome": outcome,
                "correlation_id": correlation_id,
                "camera_id": camera_id,
                "lot_ids": list(lot_ids) if lot_ids is not None else None,
            },
        )
        return [JournalEntry(**{k: getattr(r, k) for k in _J_COLUMNS.split(", ")}) for r in rows]
