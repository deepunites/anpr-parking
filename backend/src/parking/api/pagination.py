"""Курсорная пагинация: непрозрачный курсор из (created_at, id). OFFSET запрещён (фаза 3.5)."""

from __future__ import annotations

import base64
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from parking.shared.errors import ValidationError

MAX_LIMIT = 200


def encode_cursor(at: datetime, id_: UUID) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{id_}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        at, _, id_ = raw.partition("|")
        return datetime.fromisoformat(at), UUID(id_)
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("invalid cursor") from None


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
