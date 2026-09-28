"""Realtime-канал операторского UI (фаза 6 §4): сообщения WebSocket и их доставка через Redis Pub/Sub.

Канал `ws:{tenant_id}:{lot_id}` — изоляция tenant и площадки задаётся именем канала, на которое подписывается
только проверенное соединение. Redis Pub/Sub — не источник истины: клиент после переподключения перезапрашивает
состояние REST'ом, а не восстанавливает пропущенное.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis


class WsEventType(enum.StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    SESSION_CREATED = "SESSION_CREATED"
    SESSION_UPDATED = "SESSION_UPDATED"
    SESSION_CLOSED = "SESSION_CLOSED"
    PAYMENT = "PAYMENT"
    BARRIER = "BARRIER"
    OCCUPANCY = "OCCUPANCY"
    CAMERA_STATUS = "CAMERA_STATUS"
    ALERT = "ALERT"


#: Политика бэкпрешера (§4): при медленном клиенте промежуточные значения этих типов схлопываются до последнего
#: (по ключу coalesce_key) — потерять промежуточную занятость или статус камеры безопасно, последнее придёт.
COALESCED = frozenset({WsEventType.OCCUPANCY, WsEventType.CAMERA_STATUS})
#: Никогда не дропаются: если очередь клиента переполнена такими событиями — соединение закрывается кодом 1013,
#: клиент переподключается и перезапрашивает состояние. Потеря не бывает молчаливой.
NEVER_DROP = frozenset(
    {WsEventType.ALERT, WsEventType.SESSION_CREATED, WsEventType.SESSION_UPDATED, WsEventType.SESSION_CLOSED}
)
#: Остальные (ENTRY/EXIT/PAYMENT/BARRIER) при переполнении дропаются с уведомлением `GAP`, по которому клиент
#: перезапрашивает ленту.


@dataclass(frozen=True, slots=True)
class WsMessage:
    type: WsEventType
    lot_id: UUID
    data: dict[str, Any]
    ts: datetime
    event_id: str
    coalesce_key: str = ""  # для COALESCED-типов: OCCUPANCY → lot, CAMERA_STATUS → camera_id

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": self.type.value,
                "lot_id": str(self.lot_id),
                "data": self.data,
                "ts": self.ts.isoformat(),
                "event_id": self.event_id,
                "coalesce_key": self.coalesce_key,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> WsMessage:
        d = json.loads(raw)
        return cls(
            type=WsEventType(d["type"]),
            lot_id=UUID(d["lot_id"]),
            data=dict(d.get("data") or {}),
            ts=datetime.fromisoformat(d["ts"]),
            event_id=str(d.get("event_id") or ""),
            coalesce_key=str(d.get("coalesce_key") or ""),
        )


def channel(tenant_id: UUID, lot_id: UUID) -> str:
    return f"ws:{tenant_id}:{lot_id}"


@dataclass(slots=True)
class RealtimePublisher:
    redis: Redis
    published: int = field(default=0, init=False)

    async def publish(self, tenant_id: UUID, message: WsMessage) -> int:
        receivers: int = await self.redis.publish(channel(tenant_id, message.lot_id), message.to_json())
        self.published += 1
        return receivers
