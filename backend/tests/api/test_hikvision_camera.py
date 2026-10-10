"""Камера Hikvision DS-TCG406-E без отдельного реле: шлагбаум с драйвером HIKVISION_ISAPI (логин/пароль камеры
шифруются ключом клиента и не возвращаются API) и события камеры с ключом в пути (камера не умеет свой заголовок),
служебные события и проезды без номера не ломают камеру, тип события — по назначению камеры."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN, LOT_A1
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import issue_camera_key, unique_plate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fake_hikvision_camera.py"
CAM_A1_OUT = UUID("00000000-0000-4000-8000-00000000c102")
REASON = {"reason": "монтаж DS-TCG406-E"}


def _sim() -> Any:
    spec = importlib.util.spec_from_file_location("fake_hikvision_camera", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _multipart(plate: str, confidence: int = 96) -> dict[str, tuple[str, bytes, str]]:
    sim = _sim()
    return {
        "anpr.xml": ("anpr.xml", sim.anpr_xml(plate, confidence=confidence), "text/xml"),
        "licensePlatePicture.jpg": ("licensePlatePicture.jpg", sim.JPEG, "image/jpeg"),
    }


async def test_camera_barrier_keeps_credentials_secret(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    base = {"lot_id": str(LOT_A1), "name": "Стрела камеры въезда", "driver": "HIKVISION_ISAPI", **REASON}
    missing = await client.post("/api/v1/barriers", headers=admin, json={**base, "endpoint": "http://10.10.1.64"})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "CREDENTIALS_REQUIRED"
    outside = await client.post(
        "/api/v1/barriers",
        headers=admin,
        json={**base, "endpoint": "http://192.168.77.64", "username": "admin", "password": "Camera-Pass-1"},
    )
    assert outside.status_code == 422  # частный адрес не из подсетей площадки — SSRF-политика
    created = await client.post(
        "/api/v1/barriers",
        headers=admin,
        json={**base, "endpoint": "http://10.10.1.64", "username": "admin", "password": "Camera-Pass-1"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["driver"] == "HIKVISION_ISAPI" and body["has_credentials"] is True
    assert "Camera-Pass-1" not in created.text and "password" not in body
    stored = await owner.fetchval("SELECT credentials_encrypted FROM barrier WHERE id = $1", UUID(body["id"]))
    assert stored is not None and b"Camera-Pass-1" not in bytes(stored) and b"admin" not in bytes(stored)
    half = await client.patch(f"/api/v1/barriers/{body['id']}", headers=admin, json={"username": "op", **REASON})
    assert half.status_code == 422  # логин и пароль меняются только парой
    changed = await client.patch(
        f"/api/v1/barriers/{body['id']}", headers=admin, json={"username": "op", "password": "New-Pass-2", **REASON}
    )
    assert changed.status_code == 200 and changed.json()["has_credentials"] is True
    assert bytes(
        await owner.fetchval("SELECT credentials_encrypted FROM barrier WHERE id = $1", UUID(body["id"]))
    ) != bytes(stored)
    moved = await client.patch(
        f"/api/v1/barriers/{body['id']}", headers=admin, json={"endpoint": "http://10.10.1.65", **REASON}
    )
    # новый адрес без логина и пароля — нет: иначе сохранённые учётные данные ушли бы на чужой сервер
    assert moved.status_code == 422 and moved.json()["error"]["code"] == "CREDENTIALS_REQUIRED"
    moved_ok = await client.patch(
        f"/api/v1/barriers/{body['id']}",
        headers=admin,
        json={"endpoint": "http://10.10.1.65", "username": "op", "password": "New-Pass-3", **REASON},
    )
    assert moved_ok.status_code == 200, moved_ok.text
    await owner.execute("UPDATE barrier SET active = false WHERE id = $1", UUID(body["id"]))


async def test_real_camera_posts_with_key_in_path(client: httpx.AsyncClient, container: Container) -> None:
    key = await issue_camera_key(container, CAM_A1_IN)
    plate = unique_plate()
    resp = await client.post(f"/api/v1/camera/vendor/hikvision/{key}", files=_multipart(plate))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ENTRY_CONFIRMED" and resp.json()["session_id"]
    bad = await client.post("/api/v1/camera/vendor/hikvision/" + "x" * 40, files=_multipart(unique_plate()))
    assert bad.status_code == 401


async def test_heartbeat_and_no_plate_events_are_ignored(client: httpx.AsyncClient, container: Container) -> None:
    key = await issue_camera_key(container, CAM_A1_IN)
    heartbeat = (
        b'<?xml version="1.0" encoding="UTF-8"?><EventNotificationAlert version="2.0" '
        b'xmlns="http://www.hikvision.com/ver20/XMLSchema"><ipAddress>10.10.1.64</ipAddress>'
        b"<eventType>heartBeat</eventType><eventState>active</eventState></EventNotificationAlert>"
    )
    beat = await client.post(
        f"/api/v1/camera/vendor/hikvision/{key}", content=heartbeat, headers={"Content-Type": "application/xml"}
    )
    assert beat.status_code == 200 and beat.json()["status"] == "HEARTBEAT"
    stored = await container.heartbeats.read_many([CAM_A1_IN])
    assert CAM_A1_IN in stored  # контрольный сигнал камеры = камера на связи (джоб здоровья переведёт в ONLINE)
    no_plate = await client.post(f"/api/v1/camera/vendor/hikvision/{key}", files=_multipart("unknown"))
    assert no_plate.status_code == 200 and no_plate.json()["status"] == "IGNORED"


async def test_exit_camera_forward_motion_is_an_exit(client: httpx.AsyncClient, container: Container) -> None:
    """Камера выезда тоже смотрит на подъезжающие машины («forward»): событие — выезд, не DIRECTION_MISMATCH."""
    entry = await issue_camera_key(container, CAM_A1_IN)
    exit_key = await issue_camera_key(container, CAM_A1_OUT)
    plate = unique_plate()
    entered = await client.post(f"/api/v1/camera/vendor/hikvision/{entry}", files=_multipart(plate))
    assert entered.status_code == 200, entered.text
    left = await client.post(f"/api/v1/camera/vendor/hikvision/{exit_key}", files=_multipart(plate))
    assert left.status_code in {200, 202}, left.text
    assert left.json()["status"] != "DIRECTION_MISMATCH" and left.json()["status"].startswith(("EXIT", "PAYMENT"))


async def test_dahua_exit_camera_approach_is_an_exit(client: httpx.AsyncClient, container: Container) -> None:
    """Dahua на выезде видит подъезжающую машину («Approach») — это выезд по назначению камеры, не DIRECTION_MISMATCH."""
    entry = await issue_camera_key(container, CAM_A1_IN)
    exit_key = await issue_camera_key(container, CAM_A1_OUT)
    plate = unique_plate()

    def payload(direction: str) -> dict[str, Any]:
        return {"Picture": {"Plate": {"PlateNumber": plate, "Confidence": 95}, "SnapInfo": {"Direction": direction}}}

    entered = await client.post(f"/api/v1/camera/vendor/dahua/{entry}", json=payload("Approach"))
    assert entered.status_code == 200, entered.text
    left = await client.post(f"/api/v1/camera/vendor/dahua/{exit_key}", json=payload("Approach"))
    assert left.status_code in {200, 202}, left.text
    assert left.json()["status"] != "DIRECTION_MISMATCH" and left.json()["status"].startswith(("EXIT", "PAYMENT"))
