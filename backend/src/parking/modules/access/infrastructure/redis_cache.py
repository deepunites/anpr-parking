"""Кеш решений в Redis: HASH access:{tenant}:{plate_hash} с полем на площадку — DEL ключа инвалидирует все площадки разом."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from redis.asyncio import Redis

from parking.modules.access.domain.rules import AccessDecision, AccessKind

CHANNEL = "access:invalidate"


def _key(tenant_id: UUID, plate_hash: bytes) -> str:
    return f"access:{tenant_id}:{plate_hash.hex()}"


def _dump(decision: AccessDecision) -> str:
    return json.dumps(
        {
            "kind": decision.kind.value,
            "discount_percent": decision.discount_percent,
            "source_id": str(decision.source_id) if decision.source_id else None,
            "rule_id": str(decision.rule_id) if decision.rule_id else None,
            "reason": decision.reason,
            "valid_until": decision.valid_until.isoformat() if decision.valid_until else None,
        }
    )


def _load(raw: bytes | str) -> AccessDecision:
    d = json.loads(raw)
    return AccessDecision(
        kind=AccessKind(d["kind"]),
        discount_percent=int(d["discount_percent"]),
        source_id=UUID(d["source_id"]) if d.get("source_id") else None,
        rule_id=UUID(d["rule_id"]) if d.get("rule_id") else None,
        reason=d.get("reason"),
        valid_until=datetime.fromisoformat(d["valid_until"]) if d.get("valid_until") else None,
    )


class RedisDecisionCache:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def get(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes) -> AccessDecision | None:
        raw = await self._r.hget(_key(tenant_id, plate_hash), str(lot_id))
        return None if raw is None else _load(raw)

    async def set(
        self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, decision: AccessDecision, ttl_seconds: int
    ) -> None:
        key = _key(tenant_id, plate_hash)
        pipe = self._r.pipeline(transaction=False)
        pipe.hset(key, str(lot_id), _dump(decision))
        pipe.expire(key, ttl_seconds)  # общий TTL ключа: самый короткий из записанных решений «побеждает»
        await pipe.execute()

    async def invalidate(self, tenant_id: UUID, plate_hash: bytes) -> None:
        pipe = self._r.pipeline(transaction=False)
        pipe.delete(_key(tenant_id, plate_hash))
        pipe.publish(CHANNEL, f"{tenant_id}:{plate_hash.hex()}")
        await pipe.execute()
