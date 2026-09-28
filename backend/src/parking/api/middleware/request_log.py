"""Одна запись INFO на запрос: метод, путь (шаблон роута, не сырой URL), статус, duration_ms.

Тело и заголовки не логируются.
"""

from __future__ import annotations

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from parking.platform import metrics
from parking.platform.logging import get_logger

log = get_logger("http")


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status = status or 500
            raise
        finally:
            route = scope.get("route")
            path = getattr(route, "path", scope.get("path", ""))
            elapsed = time.perf_counter() - started
            # лейбл route — только шаблон известного роута: сырые пути (сканеры, 404) схлопываются в <unmatched>
            metrics.http_request_duration_seconds.labels(
                method=str(scope.get("method", "")),
                route=getattr(route, "path", None) or "<unmatched>",
                status_class=f"{status // 100}xx" if status else "0xx",
            ).observe(elapsed)
            log.info(
                "http.request",
                method=scope.get("method"),
                path=path,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
            )
