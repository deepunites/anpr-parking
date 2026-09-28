"""Heartbeat → только Redis; статусы ONLINE/DEGRADED/OFFLINE/SILENT считает джоб, флаш батчем, событие на смену."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import httpx

from parking.modules.devices.domain.camera import CameraStatus, compute_status
from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN
from tests.support.camera_fixtures import event_body, send_event, unique_plate


def test_compute_status_thresholds() -> None:
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    kw = {"now": now, "interval_sec": 300, "last_event_at": now, "expected_event_gap": None}
    assert compute_status(last_heartbeat_at=now - timedelta(seconds=599), **kw) is CameraStatus.ONLINE
    assert compute_status(last_heartbeat_at=now - timedelta(seconds=600), **kw) is CameraStatus.DEGRADED
    assert compute_status(last_heartbeat_at=now - timedelta(seconds=1500), **kw) is CameraStatus.OFFLINE
    assert compute_status(last_heartbeat_at=None, **kw) is CameraStatus.OFFLINE
    silent = compute_status(
        now=now,
        interval_sec=300,
        last_heartbeat_at=now,
        last_event_at=now - timedelta(minutes=30),
        expected_event_gap=timedelta(minutes=10),
    )
    assert silent is CameraStatus.SILENT  # жива, но не распознаёт


async def test_heartbeat_and_health_job(
    client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection
) -> None:
    await owner.execute(
        "UPDATE camera SET status = 'UNKNOWN', last_heartbeat_at = NULL, last_event_at = NULL WHERE id = $1", CAM_A1_IN
    )
    await container.redis.delete(f"camera:hb:{CAM_A1_IN}")
    response = await client.post(
        "/api/v1/camera/heartbeat", json={"firmware_version": "6.2.1"}, headers={"X-Camera-Key": entry_key}
    )
    assert response.status_code == 204
    assert await owner.fetchval("SELECT last_heartbeat_at FROM camera WHERE id = $1", CAM_A1_IN) is None  # не в PG
    assert (await container.redis.hgetall(f"camera:hb:{CAM_A1_IN}"))[b"fw"] == b"6.2.1"
    assert (await send_event(client, entry_key, event_body(unique_plate()))).status_code == 200

    report = await container.camera_health.run_once()
    assert (CAM_A1_IN, CameraStatus.UNKNOWN, CameraStatus.ONLINE) in report.changed
    row = await owner.fetchrow(
        "SELECT status::text AS status, last_heartbeat_at, last_event_at FROM camera WHERE id = $1", CAM_A1_IN
    )
    assert row["status"] == "ONLINE" and row["last_heartbeat_at"] is not None and row["last_event_at"] is not None
    assert (
        await owner.fetchval("SELECT last_used_at FROM camera_api_key WHERE key_prefix = $1", entry_key[:16])
        is not None
    )
    event = await owner.fetchrow(
        "SELECT payload FROM outbox WHERE event_type = 'devices.camera.status_changed.v1' AND aggregate_id = $1 ORDER BY created_at DESC LIMIT 1",
        CAM_A1_IN,
    )
    assert (event is not None and '"new_status": "ONLINE"' in event["payload"]) or "ONLINE" in str(event["payload"])

    # камера замолчала: heartbeat в Redis истёк, в PG последний час назад → OFFLINE + событие
    await container.redis.delete(f"camera:hb:{CAM_A1_IN}")
    await owner.execute("UPDATE camera SET last_heartbeat_at = now() - interval '1 hour' WHERE id = $1", CAM_A1_IN)
    report = await container.camera_health.run_once()
    assert (CAM_A1_IN, CameraStatus.ONLINE, CameraStatus.OFFLINE) in report.changed
    assert await owner.fetchval("SELECT status::text FROM camera WHERE id = $1", CAM_A1_IN) == "OFFLINE"

    # жива, но не распознаёт: heartbeat свежий, событий нет дольше базовой линии часа → SILENT
    await client.post("/api/v1/camera/heartbeat", json={}, headers={"X-Camera-Key": entry_key})
    hour = datetime.now(tz=UTC).hour
    await container.redis.set(f"camera:baseline:{CAM_A1_IN}:{hour}", "60")
    await container.redis.hset(
        "camera:lastevent", str(CAM_A1_IN), (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    )
    await owner.execute("UPDATE camera SET last_event_at = now() - interval '1 hour' WHERE id = $1", CAM_A1_IN)
    report = await container.camera_health.run_once()
    assert (CAM_A1_IN, CameraStatus.OFFLINE, CameraStatus.SILENT) in report.changed
    print(f"\nGATE camera health: UNKNOWN→ONLINE→OFFLINE→SILENT, flushed heartbeats={report.flushed_heartbeats}")
