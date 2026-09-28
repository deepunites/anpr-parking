"""v2 этап 6 «Интеграции»: API-ключи со scopes и сроком, исходящие вебхуки (подпись, повторы, DLQ, повторная
доставка), журнал событий камер, песочница и тестовый приёмник."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from parking.modules.identity.domain.permissions import Role
from parking.modules.integrations.infrastructure.sender import HttpWebhookSender
from parking.runtime.container import Container
from parking.shared.errors import ValidationError
from tests.conftest import BARRIER_A1_IN, CAM_A1_IN, LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate

REASON = {"reason": "интеграция с 1С"}


@dataclass
class Receiver:
    """Минимальный HTTP-приёмник на 127.0.0.1: сохраняет запросы, отвечает кодом из `status`."""

    status: int = 200
    requests: list[dict[str, Any]] = field(default_factory=list)
    port: int = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append({"path": lines[0].split(" ")[1], "headers": headers, "body": body})
        writer.write(f"HTTP/1.1 {self.status} X\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode())
        await writer.drain()
        writer.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"


@pytest.fixture(autouse=True)
async def _inbox_quota(container: Container) -> None:
    """Квота 10 приёмников в час на клиента (A-98) общая для всего набора тестов — сбрасываем перед тестом."""
    await container.redis.delete(f"inbox:create:{TENANT_A}")


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    rec = Receiver()
    server = await asyncio.start_server(rec.handle, "127.0.0.1", 0)
    rec.port = server.sockets[0].getsockname()[1]
    yield rec
    server.close()


def _valid_signature(secret: str, req: dict[str, Any]) -> bool:
    signed = req["headers"]["x-parking-timestamp"].encode() + b"." + req["body"]
    expected = "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, req["headers"]["x-parking-signature"])


async def _create_hook(
    client: httpx.AsyncClient, headers: dict[str, str], url: str, events: list[str]
) -> dict[str, Any]:
    resp = await client.post("/api/v1/webhooks", headers=headers, json={"url": url, "events": events, **REASON})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _deliver(container: Container, rounds: int = 3) -> None:
    for _ in range(rounds):
        await container.outgoing_webhooks.deliver_due()


# ---------------------------------------------------------------- API-ключи
async def test_api_key_acts_only_within_scopes_and_can_be_revoked(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    catalog = (await client.get("/api/v1/integrations/catalog", headers=admin)).json()
    assert "sessions:read" in catalog["grantable"] and "session.closed" in catalog["events"]
    created = await client.post(
        "/api/v1/api-keys",
        headers=admin,
        json={"name": "1С бухгалтерия", "scopes": ["sessions:read", "lots:read"], "expires_in_days": 30, **REASON},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    secret, key_id = body["secret"], body["key"]["id"]
    assert secret.startswith("pk_") and body["key"]["prefix"] in secret and body["key"]["active"]
    listed = (await client.get("/api/v1/api-keys", headers=admin)).json()
    assert all("secret" not in k for k in listed) and any(k["id"] == key_id for k in listed)

    as_key = {"Authorization": f"Bearer {secret}"}
    assert (await client.get(f"/api/v1/lots/{LOT_A1}/sessions", headers=as_key)).status_code == 200
    assert (await client.get("/api/v1/lots", headers=as_key)).status_code == 200
    opened = await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/open", headers=as_key, json=REASON)
    assert opened.status_code == 403  # права роли создателя ключу не достаются
    assert (await client.get("/api/v1/api-keys", headers=as_key)).status_code == 403
    assert (await client.get("/api/v1/lots", headers={"Authorization": f"Bearer {secret}x"})).status_code == 401

    revoked = await client.post(f"/api/v1/api-keys/{key_id}/revoke", headers=admin, json=REASON)
    assert revoked.status_code == 204
    assert (await client.get("/api/v1/lots", headers=as_key)).status_code == 401
    audited = await owner.fetch("SELECT action FROM audit_log WHERE entity_id = $1 ORDER BY created_at", UUID(key_id))
    assert [r["action"] for r in audited] == ["API_KEY_CREATED", "API_KEY_REVOKED"]


async def test_api_key_scopes_cannot_exceed_creator_and_expire(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    unknown = await client.post(
        "/api/v1/api-keys", headers=admin, json={"name": "x-key", "scopes": ["integrations:manage"], **REASON}
    )
    assert unknown.status_code == 422
    operator = await bearer(client, users[Role.OPERATOR])
    denied = await client.post(
        "/api/v1/api-keys", headers=operator, json={"name": "x-key", "scopes": ["sessions:read"], **REASON}
    )
    assert denied.status_code == 403
    created = await client.post(
        "/api/v1/api-keys",
        headers=admin,
        json={"name": "expiring", "scopes": ["lots:read"], "expires_in_days": 1, **REASON},
    )
    secret = created.json()["secret"]
    await owner.execute(
        "UPDATE api_key SET expires_at = now() - interval '1 minute' WHERE id = $1",
        UUID(created.json()["key"]["id"]),
    )
    assert (await client.get("/api/v1/lots", headers={"Authorization": f"Bearer {secret}"})).status_code == 401


# ---------------------------------------------------------------- вебхуки
async def test_session_closed_webhook_is_signed_and_delivered(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], receiver: Receiver
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    hook = await _create_hook(client, admin, receiver.url, ["session.closed"])
    secret = hook["secret"]
    assert secret.startswith("whsec_") and hook["webhook"]["secret_hint"].startswith("whsec_")
    session_id, event_id = str(uuid4()), uuid4()
    created = await container.outgoing_webhooks.enqueue(
        TENANT_A, "parking.session.closed.v1", event_id, datetime.now(tz=UTC), {"session_id": session_id}
    )
    assert created >= 1
    again = await container.outgoing_webhooks.enqueue(
        TENANT_A, "parking.session.closed.v1", event_id, datetime.now(tz=UTC), {"session_id": session_id}
    )
    assert again == 0  # повтор события из стрима — без второй доставки
    await _deliver(container)
    mine = [r for r in receiver.requests if json.loads(r["body"])["data"].get("session_id") == session_id]
    assert len(mine) == 1
    req = mine[0]
    assert req["headers"]["x-parking-event"] == "session.closed"
    assert _valid_signature(secret, req)
    payload = json.loads(req["body"])
    assert payload["event"] == "session.closed" and payload["event_id"] == str(event_id)
    deliveries = (
        await client.get("/api/v1/webhook-deliveries", headers=admin, params={"endpoint_id": hook["webhook"]["id"]})
    ).json()
    assert deliveries[0]["status"] == "DELIVERED" and deliveries[0]["last_status"] == 200


async def test_failed_delivery_retries_then_dead_and_can_be_redelivered(
    client: httpx.AsyncClient,
    container: Container,
    users: dict[Role, Principal],
    receiver: Receiver,
    owner: asyncpg.Connection,
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    hook = await _create_hook(client, admin, receiver.url, ["payment.confirmed"])
    endpoint_id = hook["webhook"]["id"]
    receiver.status = 500
    await container.outgoing_webhooks.enqueue(
        TENANT_A, "billing.payment.confirmed.v1", uuid4(), datetime.now(tz=UTC), {"payment_id": str(uuid4())}
    )
    await _deliver(container, 1)
    [first] = (
        await client.get("/api/v1/webhook-deliveries", headers=admin, params={"endpoint_id": endpoint_id})
    ).json()
    assert first["status"] == "RETRY" and first["attempts"] == 1 and first["last_status"] == 500
    due = datetime.fromisoformat(first["next_attempt_at"]) - datetime.fromisoformat(first["created_at"])
    assert timedelta(seconds=50) < due < timedelta(minutes=2)  # первая повторная — через минуту
    for _ in range(3):  # имитация прошедшего времени: следующие попытки 5 и 30 минут
        await owner.execute(
            "UPDATE webhook_delivery SET next_attempt_at = now() - interval '1 second' WHERE endpoint_id = $1",
            UUID(endpoint_id),
        )
        await _deliver(container, 1)
    [dead] = (await client.get("/api/v1/webhook-deliveries", headers=admin, params={"endpoint_id": endpoint_id})).json()
    assert dead["status"] == "DEAD" and dead["attempts"] == 4
    dlq = (await client.get("/api/v1/webhook-deliveries", headers=admin, params={"status": "DEAD"})).json()
    assert any(d["id"] == dead["id"] for d in dlq)

    receiver.status = 200
    redo = await client.post(f"/api/v1/webhook-deliveries/{dead['id']}/redeliver", headers=admin, json=REASON)
    assert redo.status_code == 204
    await _deliver(container, 1)
    [done] = (await client.get("/api/v1/webhook-deliveries", headers=admin, params={"endpoint_id": endpoint_id})).json()
    assert done["status"] == "DELIVERED"
    hooks = (await client.get("/api/v1/webhooks", headers=admin)).json()
    mine = next(h for h in hooks if h["id"] == endpoint_id)
    assert mine["consecutive_failures"] == 0 and mine["delivered_24h"] >= 1


async def test_test_event_rotation_and_inbox_signature_check(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], receiver: Receiver
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    hook = await _create_hook(client, admin, receiver.url, ["session.created"])
    endpoint_id = hook["webhook"]["id"]
    sent = await client.post(f"/api/v1/webhooks/{endpoint_id}/test", headers=admin)
    assert sent.status_code == 200 and sent.json()["status"] == "DELIVERED", sent.text
    assert receiver.requests[-1]["headers"]["x-parking-event"] == "webhook.test"
    assert _valid_signature(hook["secret"], receiver.requests[-1])
    rotated = await client.post(f"/api/v1/webhooks/{endpoint_id}/rotate-secret", headers=admin, json=REASON)
    new_secret = rotated.json()["secret"]
    assert new_secret != hook["secret"]
    await client.post(f"/api/v1/webhooks/{endpoint_id}/test", headers=admin)
    assert _valid_signature(new_secret, receiver.requests[-1])
    assert not _valid_signature(hook["secret"], receiver.requests[-1])

    # тестовый приёмник платформы: запросы видны в панели, подпись проверена ключом подписки на этот адрес
    inbox = (await client.post("/api/v1/integrations/test-inbox", headers=admin)).json()
    path = inbox["url"].split("://", 1)[1].split("/", 1)[1]
    body = b'{"type":"webhook.test"}'
    ts = "1700000000"
    good = "sha256=" + hmac.new(b"whsec_unknown", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    got = await client.post(
        f"/{path}",
        content=body,
        headers={"X-Parking-Event": "webhook.test", "X-Parking-Timestamp": ts, "X-Parking-Signature": good},
    )
    assert got.status_code == 200
    content = (await client.get(f"/api/v1/integrations/test-inbox/{inbox['token']}", headers=admin)).json()
    assert content["items"][0]["event"] == "webhook.test"
    assert content["items"][0]["signature_valid"] is None  # на этот адрес подписки нет — проверить нечем
    assert (await client.post("/api/v1/hooks/inbox/" + "x" * 32, content=b"{}")).status_code == 404

    deleted = await client.post(f"/api/v1/webhooks/{endpoint_id}/delete", headers=admin, json=REASON)
    assert deleted.status_code == 204
    assert all(h["id"] != endpoint_id for h in (await client.get("/api/v1/webhooks", headers=admin)).json())


async def test_webhook_url_validation(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    for url in ("ftp://example.com/x", "https://user:pw@example.com/x", "not a url"):
        resp = await client.post(
            "/api/v1/webhooks", headers=admin, json={"url": url, "events": ["session.closed"], **REASON}
        )
        assert resp.status_code == 422, url
    unknown = await client.post(
        "/api/v1/webhooks",
        headers=admin,
        json={"url": "https://example.com/x", "events": ["session.exploded"], **REASON},
    )
    assert unknown.status_code == 422


async def test_sender_blocks_private_and_metadata_addresses_in_prod_mode() -> None:
    sender = HttpWebhookSender(allow_private=False)
    try:
        for url in ("http://127.0.0.1:9/x", "http://10.0.0.5/x", "http://169.254.169.254/latest/meta-data"):
            with pytest.raises(ValidationError) as exc:
                await sender.send(url, b"{}", {})
            assert exc.value.code == "WEBHOOK_ADDRESS_BLOCKED", url
    finally:
        await sender.aclose()
    dev = HttpWebhookSender(allow_private=True)
    try:
        with pytest.raises(ValidationError):  # адрес метаданных облака закрыт даже на стенде
            await dev.send("http://169.254.169.254/latest/meta-data", b"{}", {})
    finally:
        await dev.aclose()


# ---------------------------------------------------------------- журнал событий и песочница
async def test_rejected_camera_event_is_visible_in_journal_with_reason(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], entry_key: str
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    plate = unique_plate()
    rejected = await send_event(client, entry_key, event_body(plate, confidence=5))
    assert rejected.status_code == 422
    accepted = await send_event(client, entry_key, event_body(unique_plate()))
    assert accepted.status_code == 200, accepted.text
    await container.journal.flush()
    rows = (
        await client.get(
            "/api/v1/integrations/journal", headers=admin, params={"camera_id": str(CAM_A1_IN), "outcome": "REJECTED"}
        )
    ).json()
    mine = [
        r
        for r in rows
        if r["plate_masked"] and r["plate_masked"][:2] == plate[:2] and r["plate_masked"][-2:] == plate[-2:]
    ]
    assert mine, rows[:3]
    entry = mine[0]
    assert entry["status"] == "LOW_CONFIDENCE" and entry["message"] and entry["http_status"] == 422
    assert plate not in json.dumps(rows)  # номер в журнале только маской
    by_corr = (
        await client.get(
            "/api/v1/integrations/journal", headers=admin, params={"correlation_id": entry["correlation_id"]}
        )
    ).json()
    assert [r["correlation_id"] for r in by_corr] == [entry["correlation_id"]]
    viewer = await bearer(client, users[Role.VIEWER])
    assert (await client.get("/api/v1/integrations/journal", headers=viewer)).status_code == 403


async def test_sandbox_checks_hikvision_event_without_side_effects(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    plate = unique_plate()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <ipAddress>10.10.1.5</ipAddress><dateTime>{datetime.now(tz=UTC).isoformat()}</dateTime>
      <eventType>ANPR</eventType>
      <ANPR><licensePlate>{plate}</licensePlate><confidenceLevel>97</confidenceLevel><direction>forward</direction>
      <country>UZ</country></ANPR>
    </EventNotificationAlert>"""
    before = await owner.fetchval("SELECT count(*) FROM parking_session WHERE tenant_id = $1", TENANT_A)
    ok = await client.post(
        "/api/v1/integrations/sandbox",
        headers=admin,
        json={"camera_id": str(CAM_A1_IN), "vendor": "hikvision", "payload": xml},
    )
    assert ok.status_code == 200, ok.text
    result = ok.json()
    assert result["accepted"] and result["plate"] == plate and result["event_type"] == "ENTRY"
    assert result["access_kind"] == "NORMAL" and result["confidence"] == 97
    low = await client.post(
        "/api/v1/integrations/sandbox",
        headers=admin,
        json={"camera_id": str(CAM_A1_IN), "vendor": "hikvision", "payload": xml.replace(">97<", ">10<")},
    )
    assert low.status_code == 200 and not low.json()["accepted"] and low.json()["reason"]
    broken = await client.post(
        "/api/v1/integrations/sandbox",
        headers=admin,
        json={"camera_id": str(CAM_A1_IN), "vendor": "generic", "payload": '{"camera_code": "ENTRY_01"}'},
    )
    assert broken.status_code == 200 and broken.json()["reason"] == "VALIDATION_ERROR"
    after = await owner.fetchval("SELECT count(*) FROM parking_session WHERE tenant_id = $1", TENANT_A)
    assert after == before  # песочница ничего не записывает
    await container.journal.flush()
    rows = (await client.get("/api/v1/integrations/journal", headers=admin, params={"lot_id": str(LOT_A1)})).json()
    assert {"sandbox"} <= {r["source"] for r in rows}


async def test_api_reference_lists_only_key_accessible_operations(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    spec = (await client.get("/api/v1/integrations/openapi", headers=admin)).json()
    paths = spec["paths"]
    assert "/api/v1/lots/{lot_id}/sessions" in paths and "/api/v1/camera/events" in paths
    assert "/api/v1/users" not in paths and "/api/v1/api-keys" not in paths


async def test_stream_consumer_turns_catalog_events_into_deliveries(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], receiver: Receiver
) -> None:
    from parking.workers.envelope import Envelope
    from parking.workers.handlers import with_webhooks

    admin = await bearer(client, users[Role.TENANT_ADMIN])
    hook = await _create_hook(client, admin, receiver.url, ["session.closed"])
    seen: list[str] = []

    async def domain(env: Envelope) -> None:
        seen.append(env.event_type)

    handlers = with_webhooks({"parking.session.closed.v1": domain}, container)
    assert set(handlers) >= {"parking.session.closed.v1", "billing.payment.confirmed.v1"}
    session_id = uuid4()
    env = Envelope(
        event_id=uuid4(),
        event_type="parking.session.closed.v1",
        tenant_id=TENANT_A,
        aggregate_type="parking_session",
        aggregate_id=session_id,
        occurred_at=datetime.now(tz=UTC),
        correlation_id=str(uuid4()),
        trace_parent=None,
        payload={"session_id": str(session_id), "lot_id": str(LOT_A1), "billing_minutes": 42},
    )
    await handlers[env.event_type](env)
    assert seen == ["parking.session.closed.v1"]  # доменный обработчик не потерян
    [delivery] = (
        await client.get("/api/v1/webhook-deliveries", headers=admin, params={"endpoint_id": hook["webhook"]["id"]})
    ).json()
    assert delivery["event"] == "session.closed" and delivery["payload"]["data"]["billing_minutes"] == 42


# ---------------------------------------------------------------- этап 8: ограничители и автоотключение
async def test_webhook_endpoint_auto_disabled_after_three_days_of_failures(
    client: httpx.AsyncClient,
    container: Container,
    users: dict[Role, Principal],
    receiver: Receiver,
    owner: asyncpg.Connection,
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    hook = await _create_hook(client, admin, receiver.url, ["camera.status_changed"])
    endpoint_id = UUID(hook["webhook"]["id"])
    receiver.status = 503
    await container.outgoing_webhooks.enqueue(
        TENANT_A, "devices.camera.status_changed.v1", uuid4(), datetime.now(tz=UTC), {"camera_id": str(uuid4())}
    )
    await _deliver(container, 1)
    since = await owner.fetchval("SELECT failing_since FROM webhook_endpoint WHERE id = $1", endpoint_id)
    assert since is not None  # первая неудача запомнена
    await owner.execute(
        "UPDATE webhook_endpoint SET failing_since = now() - interval '4 days' WHERE id = $1", endpoint_id
    )
    await owner.execute(
        "UPDATE webhook_delivery SET next_attempt_at = now() - interval '1 second' WHERE endpoint_id = $1", endpoint_id
    )
    await _deliver(container, 1)
    mine = next(h for h in (await client.get("/api/v1/webhooks", headers=admin)).json() if h["id"] == str(endpoint_id))
    assert mine["active"] is False and mine["disabled_reason"] == "AUTO_FAILING_3D"
    # включение вручную сбрасывает счётчик и причину
    receiver.status = 200
    on = await client.patch(f"/api/v1/webhooks/{endpoint_id}", headers=admin, json={"active": True, **REASON})
    assert on.status_code == 200 and on.json()["disabled_reason"] is None and on.json()["active"] is True
    assert await owner.fetchval("SELECT failing_since FROM webhook_endpoint WHERE id = $1", endpoint_id) is None


async def test_invalid_api_keys_are_rate_limited_per_address(client: httpx.AsyncClient, container: Container) -> None:
    ip = "203.0.113.77"
    await container.redis.delete(f"apikey:fail:{ip}")
    headers = {"Authorization": "Bearer pk_" + "a" * 16 + "_" + "b" * 32, "X-Forwarded-For": ip}
    try:
        for _ in range(20):
            assert (await client.get("/api/v1/lots", headers=headers)).status_code == 401
        blocked = await client.get("/api/v1/lots", headers=headers)
        assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "RATE_LIMITED"
        other = await client.get("/api/v1/lots", headers={**headers, "X-Forwarded-For": "203.0.113.78"})
        assert other.status_code == 401  # другой адрес не наказан
    finally:
        await container.redis.delete(f"apikey:fail:{ip}", "apikey:fail:203.0.113.78")


async def test_test_inbox_creation_is_limited_per_tenant(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    quota = f"inbox:create:{TENANT_A}"
    await container.redis.set(quota, 10, ex=3600)
    try:
        resp = await client.post("/api/v1/integrations/test-inbox", headers=admin)
        assert resp.status_code == 429
    finally:
        await container.redis.delete(quota)
    assert (await client.post("/api/v1/integrations/test-inbox", headers=admin)).status_code == 200
