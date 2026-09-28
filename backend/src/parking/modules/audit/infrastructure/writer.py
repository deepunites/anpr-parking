from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.audit.application.contracts import AuditEntry
from parking.shared.correlation import get_correlation_id

_INSERT = text(
    """
    INSERT INTO audit_log (tenant_id, actor_type, actor_id, user_id, action, entity_type, entity_id, lot_id,
                           session_id, reason, payload, ip_address, user_agent, correlation_id)
    VALUES (:tenant_id, CAST(:actor_type AS actor_type), :actor_id, :user_id, :action, :entity_type, :entity_id,
            :lot_id, :session_id, :reason, CAST(:payload AS jsonb), CAST(:ip AS inet), :user_agent, :correlation_id)
    """
)


class SqlAuditWriter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, entry: AuditEntry) -> None:
        await self._session.execute(
            _INSERT,
            {
                "tenant_id": entry.tenant_id,
                "actor_type": entry.actor_type.value,
                "actor_id": entry.actor_id,
                "user_id": entry.user_id,
                "action": entry.action,
                "entity_type": entry.entity_type,
                "entity_id": entry.entity_id,
                "lot_id": entry.lot_id,
                "session_id": entry.session_id,
                "reason": entry.reason,
                "payload": json.dumps(entry.payload, default=str),
                "ip": entry.ip_address,
                "user_agent": entry.user_agent,
                "correlation_id": get_correlation_id(),
            },
        )
