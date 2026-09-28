"""Ключ объекта изображения: строгий шаблон, принадлежность tenant/lot/camera проверяется против ключа камеры,
а не тела запроса — защита от path traversal и чужих изображений (фаза 4 §1 п.9)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

CONTENT_TYPE = "image/jpeg"
_KEY_RE = re.compile(
    r"^tenant/(?P<tenant>[0-9a-f-]{36})/lot/(?P<lot>[0-9a-f-]{36})/camera/(?P<camera>[0-9a-f-]{36})/"
    r"(?P<y>\d{4})/(?P<m>\d{2})/(?P<d>\d{2})/(?P<object>[0-9a-f-]{36})\.jpg$"
)


@dataclass(frozen=True, slots=True)
class ObjectKeyParts:
    tenant_id: UUID
    lot_id: UUID
    camera_id: UUID
    object_id: UUID


def build_key(tenant_id: UUID, lot_id: UUID, camera_id: UUID, at: datetime, object_id: UUID) -> str:
    day = at.astimezone(UTC)
    return f"tenant/{tenant_id}/lot/{lot_id}/camera/{camera_id}/{day:%Y/%m/%d}/{object_id}.jpg"


def parse_key(key: str) -> ObjectKeyParts | None:
    m = _KEY_RE.match(key)
    if m is None:
        return None
    try:
        return ObjectKeyParts(UUID(m["tenant"]), UUID(m["lot"]), UUID(m["camera"]), UUID(m["object"]))
    except ValueError:
        return None


def key_belongs_to(key: str, tenant_id: UUID, lot_id: UUID, camera_id: UUID) -> bool:
    parts = parse_key(key)
    return parts is not None and (parts.tenant_id, parts.lot_id, parts.camera_id) == (tenant_id, lot_id, camera_id)
