"""Сквозной контекст запроса в contextvars: correlation_id, tenant_id, user_id, trace.

Значения автоматически попадают в каждую запись лога (structlog.contextvars) и в конверты событий.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import UUID, uuid4

import structlog

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
tenant_id_var: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)
user_id_var: ContextVar[UUID | None] = ContextVar("user_id", default=None)


def bind_log_context(**fields: object) -> None:
    """lot_id/camera_id/session_id и т. п. в контекст логов текущей задачи (§1: обязательные поля)."""
    structlog.contextvars.bind_contextvars(
        **{k: (str(v) if isinstance(v, UUID) else v) for k, v in fields.items() if v is not None}
    )


def new_correlation_id() -> str:
    return str(uuid4())


def get_correlation_id() -> str:
    """Текущий correlation_id; создаётся, если ещё не задан (например, в фоновом джобе)."""
    value = correlation_id_var.get()
    if value is None:
        value = new_correlation_id()
        correlation_id_var.set(value)
    return value


@contextmanager
def correlation_scope(
    correlation_id: str | None = None, *, tenant_id: UUID | None = None, user_id: UUID | None = None
) -> Iterator[str]:
    """Устанавливает контекст на время блока и восстанавливает прежний после."""
    tokens: list[tuple[ContextVar[object], Token[object]]] = []
    cid = correlation_id or new_correlation_id()
    tokens.append((correlation_id_var, correlation_id_var.set(cid)))  # type: ignore[arg-type]
    if tenant_id is not None:
        tokens.append((tenant_id_var, tenant_id_var.set(tenant_id)))  # type: ignore[arg-type]
    if user_id is not None:
        tokens.append((user_id_var, user_id_var.set(user_id)))  # type: ignore[arg-type]
    snapshot = structlog.contextvars.get_contextvars()
    structlog.contextvars.bind_contextvars(  # в каждую запись лога внутри блока
        correlation_id=cid,
        **({"tenant_id": str(tenant_id)} if tenant_id else {}),
        **({"user_id": str(user_id)} if user_id else {}),
    )
    try:
        yield cid
    finally:
        # всё, что привязали внутри блока (bind_log_context: lot_id, session_id, …), не утекает в следующий
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**snapshot)
        for var, token in reversed(tokens):
            var.reset(token)
