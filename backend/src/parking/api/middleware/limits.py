"""Лимит размера тела до чтения: дешёвая проверка отсекает мусор раньше парсинга (фаза 4 §1: 8 КБ для камер)."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, default_limit: int = 1_048_576, path_limits: dict[str, int] | None = None
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = path_limits or {}

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.path_limits.items():
            if path.startswith(prefix):
                return limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _reject(send, limit)
            return
        received = 0

        async def limited_receive() -> dict[str, object]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLargeError(limit)
            return message  # type: ignore[return-value]

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLargeError:
            await _reject(send, limit)


class BodyTooLargeError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"body exceeds {limit} bytes")
        self.limit = limit


async def _reject(send: Send, limit: int) -> None:
    body = f'{{"error": {{"code": "PAYLOAD_TOO_LARGE", "message": "body exceeds {limit} bytes"}}}}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
