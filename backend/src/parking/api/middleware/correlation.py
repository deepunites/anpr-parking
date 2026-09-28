"""correlation_id: из X-Correlation-Id (если это UUID — в outbox/командах/сагах колонка uuid) или новый;
эффективное значение возвращается в ответе тем же заголовком."""

from __future__ import annotations

from uuid import UUID

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from parking.shared.correlation import correlation_id_var, new_correlation_id

HEADER = b"x-correlation-id"


def _as_uuid(value: str) -> str | None:
    try:
        return str(UUID(value)) if 32 <= len(value) <= 36 else None
    except ValueError:
        return None


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers", [])).get(HEADER, b"").decode("latin-1")
        cid = _as_uuid(incoming) or new_correlation_id()
        token = correlation_id_var.set(cid)
        snapshot = structlog.contextvars.get_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=cid)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((HEADER, cid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            # поля, привязанные обработчиком (lot_id, camera_id, session_id), живут только в рамках запроса
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(**snapshot)
            correlation_id_var.reset(token)
