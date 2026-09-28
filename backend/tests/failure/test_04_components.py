"""§5 — отказы компонентов: MinIO недоступен (реальная остановка), шлагбаум не отвечает, часы камеры сдвинуты на час,
одновременный ENTRY с двух камер, дубликат через 10 минут (кеш ответа истёк → БД), replay всей DLQ без дублей."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from tests.conftest import BACKEND, LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, issue_camera_key, send_event, unique_plate
from tests.support.fakes import FakeBarrierDriver, fake_registry, no_sleep
from tests.support.parking_fixtures import entry_cmd, exit_cmd, subscription_decision

from parking.modules.devices.application.admin import CreateCameraCommand
from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.devices.domain.camera import CameraDirection
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import ExitStatus
from parking.platform import metrics
from parking.platform.events import IntegrationEvent
from parking.runtime.container import Container, build_device_repos
from parking.shared.plate import PlateNumber
from parking.workers.envelope import Envelope, stream_name
from parking.workers.outbox_publisher import OutboxPublisher
from parking.workers.runtime import maintenance_dsn
from parking.workers.stream_consumer import DLQ_STREAM, StreamConsumer

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"


def _hikvision_xml(plate: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <ipAddress>10.10.1.5</ipAddress><dateTime>{datetime.now(tz=UTC).isoformat()}</dateTime>
      <eventType>ANPR</eventType>
      <ANPR><licensePlate>{plate}</licensePlate><confidenceLevel>97</confidenceLevel><direction>forward</direction>
      <country>UZ</country></ANPR>
    </EventNotificationAlert>"""


def _minio(command: str) -> None:
    subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "minio_dev.py"), command], check=False, capture_output=True
    )


async def test_minio_unavailable_event_processed_frame_deferred_and_retried(
    minio: str, client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection
) -> None:
    await container.redis.delete("media:pending", "media:pending:order")
    failed_before = metrics.media_store_failed_total._value.get()
    _minio("stop")
    try:
        files = {
            "anpr.xml": ("anpr.xml", _hikvision_xml(unique_plate()).encode(), "application/xml"),
            "licensePlatePicture.jpg": ("p.jpg", JPEG, "image/jpeg"),
        }
        resp = await client.post("/api/v1/camera/vendor/hikvision", files=files, headers={"X-Camera-Key": entry_key})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ENTRY_CONFIRMED"
        key = await owner.fetchval("SELECT image_object_key FROM anpr_event WHERE id = $1", UUID(body["event_id"]))
        assert key, "ключ кадра выдан и записан в событие — изображение помечено как ожидающее доставки"
        assert await container.redis.llen("media:pending:order") == 1
        assert metrics.media_store_failed_total._value.get() == failed_before + 1
        sent, deferred = await container.media.retry_pending()
        assert (sent, deferred) == (0, 1), "MinIO всё ещё недоступен — кадр остаётся в очереди"
    finally:
        _minio("start")
    for _ in range(30):
        sent, deferred = await container.media.retry_pending()
        if sent == 1:
            break
        await asyncio.sleep(0.5)
    assert (sent, deferred) == (1, 0)
    assert await container.redis.llen("media:pending:order") == 0
    url = await container.object_store.presign_get(key, ttl_seconds=60)
    async with httpx.AsyncClient() as raw:
        fetched = await raw.get(url)
    assert fetched.status_code == 200 and fetched.content == JPEG
    print("\nGATE minio outage: event 200 with image key, frame deferred (pending=1), MinIO back → retried and served")


async def test_barrier_not_responding_three_retries_breaker_and_manual_review(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    dead = FakeBarrierDriver(results=[False])  # никогда не отвечает OK
    registry, driver = fake_registry(dead)
    executor = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )
    pl = PlateNumber.parse("01F111FF")
    entered = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=1)))
    exited = await container.parking.start_exit(exit_cmd(pl, decision=subscription_decision()))
    assert exited.status is ExitStatus.EXIT_CONFIRMED and exited.saga_id is not None
    failed_before = metrics.barrier_command_total.labels(lot=str(LOT_A1), command="OPEN", result="FAILED")._value.get()
    for _ in range(4):
        await container.parking.advance_saga(TENANT_A, exited.saga_id)
        pending = await owner.fetch(
            "SELECT id FROM barrier_command WHERE saga_id = $1 AND state IN ('PENDING', 'SENT')", exited.saga_id
        )
        for row in pending:
            await executor.execute(TENANT_A, row["id"])
    command = await owner.fetchrow(
        "SELECT state::text AS state, attempts FROM barrier_command WHERE saga_id = $1 ORDER BY created_at LIMIT 1",
        exited.saga_id,
    )
    assert command is not None and command["state"] == "FAILED", dict(command)
    assert driver.calls.count(("open", driver.calls[0][1])) == 3, (
        "три попытки на одну команду (tenacity внутри execute)"
    )
    saga = await owner.fetchval("SELECT state::text FROM exit_saga WHERE id = $1", exited.saga_id)
    assert saga == "MANUAL_REVIEW"
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", entered.session_id) in {
        "ACTIVE",
        "MANUAL_REVIEW",
    }
    failed_after = metrics.barrier_command_total.labels(lot=str(LOT_A1), command="OPEN", result="FAILED")._value.get()
    assert failed_after == failed_before + 1, "barrier_command_total{result=FAILED} — вход алерта BarrierFailure"
    print("\nGATE barrier dead: 3 attempts → FAILED, saga MANUAL_REVIEW, metric for BarrierFailure incremented")


async def test_camera_clock_one_hour_ahead_is_accepted_and_marked(
    client: httpx.AsyncClient, entry_key: str, owner: asyncpg.Connection, container: Container
) -> None:
    plate = unique_plate()
    ahead = datetime.now(tz=UTC) + timedelta(hours=1)
    resp = await send_event(client, entry_key, event_body(plate, event_time=ahead))
    assert resp.status_code == 200 and resp.json()["status"] == "ENTRY_CONFIRMED", resp.text
    row = await owner.fetchrow(
        "SELECT clock_skew_seconds, late FROM anpr_event WHERE id = $1", UUID(resp.json()["event_id"])
    )
    assert row is not None and 3590 <= row["clock_skew_seconds"] <= 3610 and row["late"] is False
    # гистограмма camera_clock_skew_seconds{lot} получила наблюдение > 600 с — вход алерта CameraClockSkew
    sample = metrics.camera_clock_skew_seconds.labels(lot=str(LOT_A1))
    over_600 = sample._sum.get()
    assert over_600 >= 3590
    print(
        f"\nGATE clock +1h: accepted, clock_skew_seconds={row['clock_skew_seconds']} marked; histogram sum={over_600:.0f}s"
    )


async def test_simultaneous_entry_from_two_cameras_one_session(
    client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection, clean_lot: None
) -> None:
    second = await container.devices.create_camera(
        ADMIN,
        CreateCameraCommand(
            lot_id=LOT_A1, code=f"ENTRY_{uuid4().hex[:4].upper()}", name="Въезд 2", direction=CameraDirection.IN
        ),
        reason="failure test: second entry lane",
    )
    second_key = await issue_camera_key(container, second.id)
    plate = unique_plate()
    now = datetime.now(tz=UTC)
    a, b = await asyncio.gather(
        send_event(client, entry_key, event_body(plate, event_time=now)),
        send_event(client, second_key, event_body(plate, camera_code=second.code, event_time=now)),
    )
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    statuses = sorted([a.json()["status"], b.json()["status"]])
    plate_hash = container.plate_crypto.hash(TENANT_A, PlateNumber.parse(plate))
    active = await owner.fetch(
        "SELECT s.id FROM parking_session s JOIN vehicle v ON v.id = s.vehicle_id WHERE v.plate_hash = $1 AND s.status = 'ACTIVE'",
        plate_hash,
    )
    assert len(active) == 1, f"одна ACTIVE-сессия (I-1), ответы: {statuses}"
    opened = await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", active[0]["id"])
    assert opened <= 2  # у каждой полосы свой шлагбаум может открыться; вторая сессия не создаётся
    print(f"\nGATE two cameras: statuses={statuses}, ACTIVE sessions=1")


async def test_duplicate_after_ten_minutes_same_response_no_second_session(
    client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection, clean_lot: None
) -> None:
    body = event_body(unique_plate())
    first = await send_event(client, entry_key, body)
    assert first.status_code == 200, first.text
    # «через 10 минут»: кеш ответа в Redis истёк — остаётся только таблица anpr_idempotency (7 суток)
    deleted = 0
    async for key in container.redis.scan_iter(match="anpr:idem:*"):
        deleted += await container.redis.delete(key)
    assert deleted >= 1
    again = await send_event(client, entry_key, body)
    assert again.status_code == 200 and again.json()["duplicate"] is True, again.text
    expected = {k: v for k, v in first.json().items() if k not in {"duplicate", "correlation_id"}}
    actual = {k: v for k, v in again.json().items() if k not in {"duplicate", "correlation_id"}}
    assert actual == expected, "тот же ответ из БД"
    assert (
        await owner.fetchval("SELECT count(*) FROM parking_session WHERE id = $1", UUID(first.json()["session_id"]))
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM barrier_command WHERE session_id = $1", UUID(first.json()["session_id"])
        )
        == 1
    ), "шлагбаум второй раз не открывался (I-3)"
    print("\nGATE duplicate after cache expiry: same response from anpr_idempotency, sessions=1, barrier commands=1")


async def test_replay_entire_dlq_without_duplicates(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, users: dict[Role, Principal]
) -> None:
    for shard in range(8):
        await container.redis.delete(stream_name(shard), f"lease:dlq-test:{stream_name(shard)}")
    await container.redis.delete(DLQ_STREAM)
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    broken = True
    handled: list[UUID] = []
    event_ids: list[UUID] = []

    async def handler(env: Envelope) -> None:
        if broken:
            raise RuntimeError("downstream unavailable")
        handled.append(env.aggregate_id)
        event_ids.append(env.event_id)

    consumer = StreamConsumer(
        container.database,
        container.redis,
        {"parking.session.orphaned.v1": handler},
        group="dlq-test",
        consumer_name="c1",
        claim_idle_ms=1,
        max_attempts=2,
    )
    await consumer.ensure_groups()
    publisher = OutboxPublisher(
        container.database, container.redis, maintenance_dsn=maintenance_dsn(container.settings), batch_size=500
    )
    try:
        ids = [uuid4() for _ in range(12)]
        async with container.uow(TENANT_A) as uow:
            for aggregate in ids:
                uow.publish(
                    IntegrationEvent(
                        type="parking.session.orphaned.v1",
                        tenant_id=TENANT_A,
                        aggregate_type="parking_session",
                        aggregate_id=aggregate,
                        payload={
                            "session_id": str(aggregate),
                            "lot_id": str(LOT_A1),
                            "vehicle_id": str(uuid4()),
                            "entry_time": datetime.now(tz=UTC).isoformat(),
                            "status": "ORPHAN",
                            "reason": "dlq",
                        },
                    )
                )
        while await publisher.publish_batch():
            pass
        for _ in range(60):  # повтор ждёт в отложенной очереди 1 с (бэкофф), затем вторая попытка → DLQ
            await consumer.poll_once(block_ms=1)
            if await container.redis.xlen(DLQ_STREAM) == 12:
                break
            await asyncio.sleep(0.1)
        assert await container.redis.xlen(DLQ_STREAM) == 12, "все 12 сообщений после max_attempts в DLQ"
        broken = False
        headers = await bearer(client, users[Role.PLATFORM_ADMIN])
        replayed = await client.post(
            "/api/v1/admin/dlq/replay", json={"stream": "events", "reason": "replay all after fix"}, headers=headers
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["count"] == 12
        for _ in range(6):
            await consumer.poll_once(block_ms=1)
        # повторный replay (оператор нажал дважды / DLQ пуст) — ничего не дублирует
        second = await client.post(
            "/api/v1/admin/dlq/replay", json={"stream": "events", "reason": "second click"}, headers=headers
        )
        assert second.status_code == 200 and second.json()["count"] == 0
        for _ in range(3):
            await consumer.poll_once(block_ms=1)
    finally:
        await publisher.release()
    assert sorted(handled) == sorted(ids) and len(handled) == 12, (
        f"каждое событие обработано ровно один раз: {len(handled)}"
    )
    assert await container.redis.xlen(DLQ_STREAM) == 0
    inbox = await owner.fetchval(
        "SELECT count(*) FROM consumer_inbox WHERE consumer_group = 'dlq-test' AND event_id = ANY($1::uuid[])",
        event_ids,
    )
    assert inbox == 12
    print("\nGATE dlq replay: 12 messages in DLQ → replay all → 12 handled once, second replay = 0, DLQ empty")


@pytest.mark.parametrize("scenario", ["PostgresDiskAlmostFull", "CameraClockSkew", "BarrierFailure"])
def test_alert_rules_for_failure_scenarios_are_unit_tested(scenario: str) -> None:
    """Диск 95 %, сдвиг часов, отказ шлагбаума: срабатывание правил проверяется promtool'ом на синтетических рядах
    (реальный диск не заполняем) — см. deploy/prometheus/alerts_test.yaml."""
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    tests_yaml = (root / "deploy/prometheus/alerts_test.yaml").read_text()
    assert f"alertname: {scenario}" in tests_yaml
    promtool = shutil.which("promtool") or str(Path.home() / ".local/prometheus/bin/promtool")
    if not Path(promtool).exists():
        pytest.skip("promtool не установлен")
    result = subprocess.run(
        [promtool, "test", "rules", str(root / "deploy/prometheus/alerts_test.yaml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
