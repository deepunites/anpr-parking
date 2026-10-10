"""Оффлайн-режим (фаза 4 §4): PostgreSQL остановлен по-настоящему (pg_ctl stop) → 202 + Redis Stream;
поднят → replay без дублей; consumer убит на середине → XAUTOCLAIM; DLQ после N попыток + replay через админ-API."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest

from parking.modules.anpr.application.ingest import IngestService
from parking.modules.anpr.domain.event import CameraEvent, EventType, idempotency_key
from parking.modules.devices.application.ports import BarrierInfo
from parking.modules.identity.application.contracts import Role
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import BACKEND, BARRIER_A1_IN, CAM_A1_IN, LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate
from tests.support.fakes import FakeBarrierDriver, no_sleep

PG_DEV = [sys.executable, str(BACKEND / "scripts" / "pg_dev.py")]


async def _pg(command: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *PG_DEV, command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()[-500:]


def _envelope(container: Container, plate: str, *, event_type: EventType = EventType.ENTRY) -> dict[str, str]:
    now = datetime.now(tz=UTC)
    event = CameraEvent(
        event_id=uuid4(),
        tenant_id=TENANT_A,
        lot_id=LOT_A1,
        camera_id=CAM_A1_IN,
        camera_code="ENTRY_01",
        event_type=event_type,
        plate=PlateNumber.parse(plate),
        confidence=95,
        event_time=now,
        received_at=now,
        image_object_key=None,
        correlation_id=str(uuid4()),
    )
    key = idempotency_key(TENANT_A, CAM_A1_IN, event_type, container.plate_crypto.hash(TENANT_A, event.plate), now)
    return IngestService._envelope(event, key, barrier_opened=False)


async def _drain(container: Container, replayer: Any, *, attempts: int = 60) -> list[Any]:
    reports = []
    for _ in range(attempts):
        report = await replayer.poll_once()
        reports.append(report)
        if await container.offline_buffer.pending() == 0 and await container.offline_buffer.length() == 0:
            break
        await asyncio.sleep(0.2)
    return reports


# pg_ctl останавливает только локальный кластер (backend/.pgdata); внешний (PARKING_TEST_DSN — сервис CI) тест
# остановить не может, и события честно доходят до БД (200 вместо 202). Гейт оффлайна — на локальном кластере.
@pytest.mark.skipif(
    bool(os.environ.get("PARKING_TEST_DSN")), reason="нужен локальный кластер, который тест останавливает"
)
async def test_pg_down_queues_events_and_replay_has_no_duplicates(
    client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection, clean_streams: None
) -> None:
    """Gate: реальная остановка PostgreSQL. Ответы 202 queued, XLEN = число событий; после старта — все в БД,
    дублей нет (сверка по счётчикам anpr_event/parking_session/anpr_idempotency)."""
    fake = FakeBarrierDriver()
    container.drivers._drivers["MODBUS_TCP"] = fake  # оффлайн-открытие через кеш шлагбаума (A-10)
    await container.ingest._barrier_cache.remember(
        BarrierInfo(
            BARRIER_A1_IN, LOT_A1, "MODBUS_TCP", "10.10.1.20:502", None, True, CAM_A1_IN, "Въезд", ("10.10.1.0/24",)
        )
    )
    container.db_breaker.ttl_seconds = 1.0
    online = await send_event(client, entry_key, event_body(unique_plate()))
    assert online.status_code == 200
    before_events = await owner.fetchval("SELECT count(*) FROM anpr_event")
    before_sessions = await owner.fetchval("SELECT count(*) FROM parking_session")
    await owner.close()  # владелец соединения закрывается перед остановкой кластера
    plates = [unique_plate() for _ in range(10)]
    bodies = [event_body(p) for p in plates]
    await _pg("stop")
    try:
        queued = [await send_event(client, entry_key, b) for b in bodies]
        duplicate = await send_event(client, entry_key, bodies[0])
        statuses = [r.status_code for r in queued]
        assert statuses == [202] * 10, [r.text for r in queued if r.status_code != 202]
        assert all(r.json()["queued"] is True and r.json()["status"] == "QUEUED" for r in queued)
        assert duplicate.status_code == 202  # оффлайн дедупликации нет: ключ едет в конверте, дубль снимет replay
        assert await container.offline_buffer.length() == 11
        assert container.db_breaker.is_open
        assert len([c for c in fake.calls if c[0] == "open"]) == 11  # ENTRY в оффлайне открывает шлагбаум по кешу
        print(
            f"\nGATE offline: pg_ctl stop → {statuses.count(202)}×202 queued=true, XLEN anpr:offline = 11, breaker={container.db_breaker.state.value}"
        )
    finally:
        await _pg("start")
    await asyncio.sleep(1.2)  # TTL breaker → HALF_OPEN проба
    replayer = container.offline_replayer(consumer_name="test-replayer", sleep=no_sleep, claim_idle_ms=0)
    reports = await _drain(container, replayer)
    processed = sum(r.processed for r in reports)
    duplicates = sum(r.duplicates for r in reports)
    conn = await asyncpg.connect(_dsn())
    try:
        after_events = await conn.fetchval("SELECT count(*) FROM anpr_event")
        after_sessions = await conn.fetchval("SELECT count(*) FROM parking_session")
        queued_rows = await conn.fetchval(
            "SELECT count(*) FROM anpr_event WHERE queued_offline AND received_at > now() - interval '5 minutes'"
        )
        audit = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'BARRIER_OPEN_OFFLINE' AND created_at > now() - interval '5 minutes'"
        )
    finally:
        await conn.close()
    assert (processed, duplicates) == (11, 1)
    assert after_events - before_events == 10 and after_sessions - before_sessions == 10
    assert queued_rows == 10 and audit == 10
    assert await container.offline_buffer.length() == 0 or await container.offline_buffer.pending() == 0
    assert not container.db_breaker.is_open
    print(
        f"GATE replay: processed={processed} duplicates={duplicates}; anpr_event +{after_events - before_events}, sessions +{after_sessions - before_sessions}, queued_offline rows={queued_rows}, offline barrier audit={audit}"
    )


def _dsn() -> str:
    from tests.conftest import PGDATA

    external = os.environ.get("PARKING_TEST_DSN")
    if external:
        from tests.conftest import _with_db

        return _with_db(external, "parking_test")
    return f"postgresql://postgres@/parking_test?host={PGDATA}"


async def test_consumer_killed_mid_batch_is_recovered_by_xautoclaim(
    container: Container, owner: asyncpg.Connection, clean_streams: None
) -> None:
    plates = [unique_plate() for _ in range(5)]
    for plate in plates:
        await container.offline_buffer.enqueue(_envelope(container, plate))
    await container.offline_buffer.ensure_group()
    taken = await container.offline_buffer.read(
        "dead-consumer", count=10, own_pending=False
    )  # прочитал и «умер» без ACK
    assert len(taken) == 5 and await container.offline_buffer.pending() == 5
    replayer = container.offline_replayer(consumer_name="survivor", sleep=no_sleep, claim_idle_ms=0)
    reports = await _drain(container, replayer)
    assert sum(r.processed for r in reports) == 5
    assert await container.offline_buffer.pending() == 0
    hashes = [container.plate_crypto.hash(TENANT_A, PlateNumber.parse(p)) for p in plates]
    stored = await owner.fetchval("SELECT count(*) FROM anpr_event WHERE plate_hash = ANY($1::bytea[])", hashes)
    assert stored == 5
    print("\nGATE xautoclaim: 5 pending от убитого consumer'а подобраны, потерь нет (5/5 в anpr_event)")


async def test_poison_message_goes_to_dlq_and_admin_replay(
    client: httpx.AsyncClient,
    container: Container,
    owner: asyncpg.Connection,
    users: dict[Role, Principal],
    clean_streams: None,
) -> None:
    poison = _envelope(container, unique_plate())
    poison["plate_number"] = "!!"  # не разбирается → ошибка данных, не связи: ретраи → DLQ
    await container.offline_buffer.enqueue(poison)
    replayer = container.offline_replayer(consumer_name="dlq-test", sleep=no_sleep, claim_idle_ms=0, max_attempts=3)
    reports = [await replayer.poll_once() for _ in range(4)]
    counts = [(r.processed, r.failed, r.dead_lettered) for r in reports]
    assert sum(r.failed for r in reports) == 3, counts
    assert sum(r.dead_lettered for r in reports) == 1, counts
    assert await container.offline_buffer.length() == 0 or await container.offline_buffer.pending() == 0
    headers = await bearer(client, users[Role.PLATFORM_ADMIN])
    listed = await client.get("/api/v1/admin/dlq", params={"stream": "anpr"}, headers=headers)
    assert listed.status_code == 200 and len(listed.json()) == 1
    entry = listed.json()[0]
    assert entry["fields"]["retry_count"] == "2" and entry["fields"]["plate_number"] != "!!"  # маскировано
    dry = await client.post(
        "/api/v1/admin/dlq/replay", json={"stream": "anpr", "dry_run": True, "reason": "dry run check"}, headers=headers
    )
    assert dry.status_code == 200 and dry.json()["count"] == 1 and dry.json()["dry_run"] is True
    assert len((await client.get("/api/v1/admin/dlq", headers=headers)).json()) == 1  # dry-run ничего не тронул
    forbidden = await client.post(
        "/api/v1/admin/dlq/replay",
        json={"stream": "anpr", "reason": "no rights"},
        headers=await bearer(client, users[Role.TENANT_ADMIN]),
    )
    assert forbidden.status_code == 403
    real = await client.post(
        "/api/v1/admin/dlq/replay",
        json={"stream": "anpr", "ids": [entry["id"]], "reason": "fixed upstream"},
        headers=headers,
    )
    assert real.status_code == 200 and real.json()["count"] == 1
    assert (await client.get("/api/v1/admin/dlq", headers=headers)).json() == []
    assert await container.offline_buffer.length() == 1  # вернулась в anpr:offline с retry_count=0
    replayed = await container.redis.xrange("anpr:offline", count=1)
    assert replayed[0][1][b"retry_count"] == b"0" and replayed[0][1][b"replayed_from"] == entry["id"].encode()
    audit = await owner.fetch(
        "SELECT action, reason FROM audit_log WHERE action LIKE 'DLQ_REPLAY%' ORDER BY created_at"
    )
    assert [a["action"] for a in audit][-2:] == ["DLQ_REPLAY_DRY_RUN", "DLQ_REPLAY"]
    print(
        "\nGATE dlq: 3 попытки → anpr:dlq; GET /admin/dlq; replay dry_run не меняет; replay → anpr:offline, аудит DLQ_REPLAY"
    )
