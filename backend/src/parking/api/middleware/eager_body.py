"""Тело запроса камеры читается сразу, до проверки ключа и любых await обработчика.

Камеры Hikvision (cpp-httplib) закрывают соединение через ~30 мс после отправки, не дожидаясь ответа. uvicorn после
закрытия (даже половинного) отдаёт приложению http.disconnect вместо уже полученного тела, и обработчик падал на
ClientDisconnect, хотя событие дошло целиком. Прочитанное тело воспроизводится обработчику; ответ камере, которая уже
отключилась, uvicorn молча не отправляет, а событие обработано и записано в журнал.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class EagerBodyMiddleware:
    def __init__(self, app: ASGIApp, *, prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.prefixes = prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # клиент ушёл, не дослав тело — разбирать нечего, обработчик увидит обрыв как раньше
                await self.app(scope, _replay([message], receive), send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body: Message = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        await self.app(scope, _replay([body], receive), send)


def _replay(messages: list[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay
