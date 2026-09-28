"""WebSocket операторского UI (фаза 6 §4): auth первым сообщением, изоляция tenant/скоупа, доставка через Redis Pub/Sub,
heartbeat, переавторизация без разрыва, политика бэкпрешера."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis as redis_sync
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from parking.api.app import create_app
from parking.api.ws import CLOSE_AUTH, CLOSE_NOT_FOUND, CLOSE_OVERLOADED, QUEUE_SIZE, LotSocketSession
from parking.modules.identity.application.contracts import Role
from parking.platform.config import DevSettings
from parking.platform.realtime import WsEventType, WsMessage, channel
from parking.runtime.container import Container
from tests.conftest import LOT_A1, LOT_A2, LOT_B1, TENANT_A
from tests.support.api_fixtures import PASSWORD, Principal


@pytest.fixture
def tc(
    api_settings: DevSettings, users: dict[Role, Principal], tenant_b_admin: Principal, scoped_operator: Principal
) -> Iterator[TestClient]:
    """Отдельное приложение с собственным lifespan: контейнер и пулы живут в event loop TestClient'а."""
    with TestClient(create_app(api_settings)) as client:
        yield client


def _login(tc: TestClient, principal: Principal) -> str:
    response = tc.post("/api/v1/auth/login", json={"email": principal.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


@contextlib.contextmanager
def _connect(tc: TestClient, lot_id: UUID, token: str) -> Iterator[WebSocketTestSession]:
    with tc.websocket_connect(f"/ws/v1/lots/{lot_id}") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        yield ws


def _recv_until(ws: WebSocketTestSession, kind: str, *, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        message = json.loads(ws.receive_text())
        if message.get("type") == kind:
            return dict(message)
    raise AssertionError(f"no {kind} message received")


def _publish(api_settings: DevSettings, tenant_id: UUID, message: WsMessage) -> int:
    client = redis_sync.Redis.from_url(api_settings.redis_url.get_secret_value())
    try:
        return int(client.publish(channel(tenant_id, message.lot_id), message.to_json()))
    finally:
        client.close()


def _msg(event: WsEventType, lot_id: UUID = LOT_A1, **data: Any) -> WsMessage:
    return WsMessage(
        type=event,
        lot_id=lot_id,
        data=data,
        ts=datetime.now(tz=UTC),
        event_id=str(uuid4()),
        coalesce_key=str(data.get("camera_id", lot_id)),
    )


def test_auth_is_first_message_not_query_string(tc: TestClient, users: dict[Role, Principal]) -> None:
    token = _login(tc, users[Role.OPERATOR])
    with pytest.raises(WebSocketDisconnect) as exc, tc.websocket_connect(f"/ws/v1/lots/{LOT_A1}?token={token}") as ws:
        ws.send_text(json.dumps({"type": "ping"}))  # первым должен быть auth
        ws.receive_text()
    assert exc.value.code == CLOSE_AUTH
    with pytest.raises(WebSocketDisconnect) as exc2, tc.websocket_connect(f"/ws/v1/lots/{LOT_A1}") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "garbage"}))
        ws.receive_text()
        ws.receive_text()
    assert exc2.value.code == CLOSE_AUTH


def test_delivery_heartbeat_and_reauth(tc: TestClient, api_settings: DevSettings, users: dict[Role, Principal]) -> None:
    token = _login(tc, users[Role.OPERATOR])
    with _connect(tc, LOT_A1, token) as ws:
        ok = _recv_until(ws, "auth_ok")
        assert ok["lot_id"] == str(LOT_A1) and ok["expires_at"] > datetime.now(tz=UTC).timestamp()
        _recv_until(ws, "ready")  # подписка на канал установлена — только теперь публикации доходят
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_until(ws, "pong")["type"] == "pong"
        session_id = str(uuid4())
        assert (
            _publish(api_settings, TENANT_A, _msg(WsEventType.SESSION_CREATED, session_id=session_id, status="ACTIVE"))
            == 1
        )
        got = _recv_until(ws, "SESSION_CREATED")
        assert got["data"]["session_id"] == session_id and got["lot_id"] == str(LOT_A1)
        # чужая площадка того же tenant — не доставляется (подписка только на свой канал)
        _publish(api_settings, TENANT_A, _msg(WsEventType.ALERT, lot_id=LOT_A2, kind="MANUAL_REVIEW"))
        _publish(
            api_settings, TENANT_A, _msg(WsEventType.ALERT, lot_id=LOT_A1, kind="MANUAL_REVIEW", session_id=session_id)
        )
        alert = _recv_until(ws, "ALERT")
        assert alert["lot_id"] == str(LOT_A1)
        # переавторизация без разрыва: новый токен → auth_ok; мусор → error, соединение живо
        ws.send_text(json.dumps({"type": "auth", "token": _login(tc, users[Role.OPERATOR])}))
        assert _recv_until(ws, "auth_ok")["user_id"] == str(users[Role.OPERATOR].user_id)
        ws.send_text(json.dumps({"type": "auth", "token": "nope"}))
        assert _recv_until(ws, "error")["code"] == "UNAUTHENTICATED"
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_until(ws, "pong")["type"] == "pong"
    print("\nGATE ws: auth first message, Pub/Sub delivery, lot isolation, ping/pong, re-auth without disconnect")


def test_tenant_and_scope_isolation(
    tc: TestClient, tenant_b_admin: Principal, scoped_operator: Principal, users: dict[Role, Principal]
) -> None:
    """Пользователь tenant B не подключится к площадке tenant A; оператор со скоупом A2 — к A1."""
    for principal, lot in ((tenant_b_admin, LOT_A1), (scoped_operator, LOT_A2), (users[Role.OPERATOR], LOT_B1)):
        with pytest.raises(WebSocketDisconnect) as exc, _connect(tc, lot, _login(tc, principal)) as ws:
            ws.receive_text()
            ws.receive_text()
        assert exc.value.code == CLOSE_NOT_FOUND, (principal.email, lot)
    print("\nGATE ws isolation: tenant B → 4404 on tenant A lot; scoped operator → 4404 outside scope")


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: int | None = None

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def test_backpressure_policy(container: Container) -> None:
    """OCCUPANCY схлопывается до последнего; ENTRY при переполнении дропается с GAP; SESSION_* не дропается — 1013."""
    session = LotSocketSession(_FakeSocket(), container, LOT_A1)  # type: ignore[arg-type]
    for occupied in range(100):
        session.enqueue(_msg(WsEventType.OCCUPANCY, occupied=occupied))
    assert session._queue.qsize() == 1 and session._coalesced[f"OCCUPANCY:{LOT_A1}"].data["occupied"] == 99
    for i in range(QUEUE_SIZE + 10):
        session.enqueue(_msg(WsEventType.ENTRY, session_id=str(i)))
    assert session.dropped == 11 and session._gap_pending and not session._closed
    for _ in range(3):
        session.enqueue(_msg(WsEventType.SESSION_CREATED, session_id="x"))
    assert session._closed and session._close_code == CLOSE_OVERLOADED

    async def drain() -> None:
        fake = _FakeSocket()
        s2 = LotSocketSession(fake, container, LOT_A1)  # type: ignore[arg-type]
        for occupied in (1, 2, 3):
            s2.enqueue(_msg(WsEventType.OCCUPANCY, occupied=occupied))
        s2.enqueue(_msg(WsEventType.SESSION_CLOSED, session_id="s"))
        task = asyncio.create_task(s2._sender())
        await asyncio.sleep(0.2)
        task.cancel()
        kinds = [json.loads(m)["type"] for m in fake.sent]
        assert kinds[:2] == ["OCCUPANCY", "SESSION_CLOSED"] and json.loads(fake.sent[0])["data"]["occupied"] == 3

    await drain()
    print(
        f"\nGATE ws backpressure: 100 OCCUPANCY → 1 (latest), ENTRY overflow dropped={session.dropped} + GAP, SESSION_* overflow → close {CLOSE_OVERLOADED}"
    )
