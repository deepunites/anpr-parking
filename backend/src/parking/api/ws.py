"""WebSocket операторского UI: `/ws/v1/lots/{lot_id}` (фаза 6 §4).

- Аутентификация — первым сообщением `{"type": "auth", "token": …}` (не в query string: токен не должен попасть
  в логи прокси), 5 с на него; проверка доступа к площадке и tenant — `container.lots.get(user, lot_id)` (чужая
  площадка → 404 → код 4404, вне скоупа — так же). Подписка только на канал `ws:{tenant}:{lot}` этого tenant.
- Heartbeat в обе стороны: сервер шлёт `ping` каждые 20 с и ждёт `pong`/любое сообщение в течение 40 с; клиент
  может слать `ping` — сервер отвечает `pong`.
- Бэкпрешер (platform.realtime): OCCUPANCY/CAMERA_STATUS схлопываются, ENTRY/EXIT/PAYMENT/BARRIER при переполнении
  дропаются с `GAP`, ALERT/SESSION_* не дропаются никогда — при переполнении соединение закрывается 1013.
- Переавторизация без разрыва: за 60 с до истечения access-токена сервер шлёт `auth_expiring`, клиент присылает
  новый `auth`; если токен истёк и нового нет 30 с — 4401.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from parking.modules.identity.application.contracts import CurrentUser
from parking.platform import metrics
from parking.platform.logging import get_logger
from parking.platform.realtime import COALESCED, NEVER_DROP, WsEventType, WsMessage, channel
from parking.runtime.container import Container
from parking.shared.errors import AuthenticationError, AuthorizationError, DomainError, NotFoundError

log = get_logger("api.ws")
router = APIRouter()

AUTH_TIMEOUT = 5.0
PING_INTERVAL = 20.0
PONG_TIMEOUT = 40.0
EXPIRY_WARNING = 60.0
EXPIRY_GRACE = 30.0
QUEUE_SIZE = 256

CLOSE_AUTH = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_OVERLOADED = 1013
CLOSE_INTERNAL = 1011

_COALESCE_MARK = object()
_GAP_MARK = object()


class LotSocketSession:
    def __init__(self, websocket: WebSocket, container: Container, lot_id: UUID) -> None:
        self._ws = websocket
        self._container = container
        self._lot_id = lot_id
        self._user: CurrentUser | None = None
        self._expires_at = 0.0
        self._warned = False
        self._last_seen = time.monotonic()
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._coalesced: dict[str, WsMessage] = {}
        self._closed = False
        self.dropped = 0

    async def run(self) -> None:
        await self._ws.accept()
        if not await self._authenticate_first():
            return
        metrics.ws_connections.inc()
        tasks = [
            asyncio.create_task(self._pump(), name="ws-pump"),
            asyncio.create_task(self._sender(), name="ws-sender"),
            asyncio.create_task(self._receiver(), name="ws-receiver"),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    log.error("ws_task_failed", task=task.get_name(), error_type=type(exc).__name__)
                    await self._close(CLOSE_INTERNAL)
        finally:
            self._closed = True
            for task in tasks:
                task.cancel()
            metrics.ws_connections.dec()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- auth ----------

    async def _authenticate_first(self) -> bool:
        try:
            raw = await asyncio.wait_for(self._ws.receive_text(), timeout=AUTH_TIMEOUT)
        except (TimeoutError, WebSocketDisconnect):
            await self._close(CLOSE_AUTH)
            return False
        message = _parse(raw)
        if message.get("type") != "auth":
            await self._close(CLOSE_AUTH)
            return False
        return await self._apply_auth(str(message.get("token") or ""), first=True)

    async def _apply_auth(self, token: str, *, first: bool) -> bool:
        try:
            user = await self._container.auth.authenticate(token)
            if user.must_change_password:  # временный пароль: живые данные — только после смены
                raise AuthorizationError("password change required", code="PASSWORD_CHANGE_REQUIRED")
            claims = self._container.tokens.verify(token, self._container.clock.now())
            await self._container.lots.get(user, self._lot_id)  # чужой tenant / вне скоупа → NotFoundError
        except AuthenticationError:
            await self._send({"type": "error", "code": "UNAUTHENTICATED"})
            if first:
                await self._close(CLOSE_AUTH)
            return False
        except NotFoundError:
            await self._close(CLOSE_NOT_FOUND)
            return False
        except DomainError as exc:
            log.warning("ws_auth_failed", code=exc.code)
            await self._close(CLOSE_FORBIDDEN)
            return False
        if self._user is not None and (user.tenant_id != self._user.tenant_id or user.user_id != self._user.user_id):
            await self._close(CLOSE_FORBIDDEN)  # переавторизация другим пользователем — не смена, а разрыв
            return False
        self._user = user
        self._expires_at = float(str(claims.get("exp") or 0))
        self._warned = False
        await self._send(
            {
                "type": "auth_ok",
                "user_id": str(user.user_id),
                "lot_id": str(self._lot_id),
                "expires_at": self._expires_at,
            }
        )
        return True

    # ---------- pump: Redis Pub/Sub → очередь с политикой бэкпрешера ----------

    async def _pump(self) -> None:
        assert self._user is not None
        pubsub = self._container.redis.pubsub()
        await pubsub.subscribe(channel(self._user.tenant_id, self._lot_id))
        await self._send(
            {"type": "ready"}
        )  # с этого момента события площадки доставляются; клиент перезапрашивает состояние
        try:
            while not self._closed:
                item = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if item is None or item.get("type") != "message":
                    continue
                self.enqueue(WsMessage.from_json(item["data"]))
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pubsub.aclose(), timeout=1.0)  # type: ignore[no-untyped-call]

    def enqueue(self, message: WsMessage) -> None:
        """Политика §4: COALESCED — последнее значение по ключу; NEVER_DROP — переполнение = закрыть 1013; прочие —
        дроп с GAP-уведомлением."""
        if message.type in COALESCED:
            key = f"{message.type.value}:{message.coalesce_key}"
            fresh = key not in self._coalesced
            self._coalesced[key] = message
            if fresh:
                try:
                    self._queue.put_nowait((_COALESCE_MARK, key))
                except asyncio.QueueFull:
                    pass  # значение останется в _coalesced и уйдёт со следующей меткой
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            if message.type in NEVER_DROP:
                metrics.ws_overloaded_total.inc()
                log.warning("ws_overloaded_close", lot_id=str(self._lot_id), type=message.type.value)
                self._closed = True
                self._close_code = CLOSE_OVERLOADED
                return
            self.dropped += 1
            metrics.ws_dropped_total.labels(type=message.type.value).inc()
            self._gap_pending = True

    _close_code: int | None = None
    _gap_pending: bool = False

    # ---------- sender: очередь → сокет, heartbeat, истечение токена ----------

    async def _sender(self) -> None:
        next_ping = time.monotonic() + PING_INTERVAL
        while True:
            if self._closed:
                await self._close(self._close_code or CLOSE_INTERNAL)
                return
            now = time.monotonic()
            if now - self._last_seen > PONG_TIMEOUT:
                log.info("ws_dead_connection", lot_id=str(self._lot_id))
                await self._close(1001)
                return
            if self._expires_at:
                remaining = self._expires_at - time.time()
                if remaining <= -EXPIRY_GRACE:
                    await self._send({"type": "error", "code": "TOKEN_EXPIRED"})
                    await self._close(CLOSE_AUTH)
                    return
                if remaining <= EXPIRY_WARNING and not self._warned:
                    self._warned = True
                    await self._send({"type": "auth_expiring", "expires_at": self._expires_at})
            if now >= next_ping:
                await self._send({"type": "ping", "ts": datetime.now(tz=UTC).isoformat()})
                next_ping = now + PING_INTERVAL
            if self._gap_pending:
                self._gap_pending = False
                await self._send({"type": "GAP", "dropped": self.dropped})
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(item, tuple) and item[0] is _COALESCE_MARK:
                message = self._coalesced.pop(str(item[1]), None)
                if message is not None:
                    await self._ws.send_text(message.to_json())
            elif isinstance(item, WsMessage):
                await self._ws.send_text(item.to_json())
            metrics.ws_messages_total.inc()

    # ---------- receiver: pong/ping/auth от клиента ----------

    async def _receiver(self) -> None:
        while True:
            raw = await self._ws.receive_text()
            self._last_seen = time.monotonic()
            message = _parse(raw)
            kind = message.get("type")
            if kind == "ping":
                await self._send({"type": "pong"})
            elif kind == "auth":
                await self._apply_auth(str(message.get("token") or ""), first=False)
                if self._closed:  # чужой пользователь/площадка — соединение закрыто; невалидный токен — только error
                    return
            elif kind not in {"pong", None}:
                await self._send({"type": "error", "code": "UNKNOWN_MESSAGE"})

    # ---------- общее ----------

    async def _send(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self._ws.send_text(json.dumps(payload, default=str))

    async def _close(self, code: int) -> None:
        self._closed = True
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self._ws.close(code=code)


def _parse(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.websocket("/ws/v1/lots/{lot_id}")
async def lot_socket(websocket: WebSocket, lot_id: UUID) -> None:
    container: Container = websocket.app.state.container
    try:
        await LotSocketSession(websocket, container, lot_id).run()
    except asyncio.CancelledError:  # сервер/тест-клиент сворачивает соединение — обработчик и так завершён
        return


__all__ = ["LotSocketSession", "WsEventType", "router"]
