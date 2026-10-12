"""Круглосуточная запись камер на диск сервера площадки: deploy/local/stand/camera_streams.py берёт логин и пароль
камеры у её стрелы Hikvision (зашифрованы ключом клиента) и собирает RTSP-адрес потока — второй раз пароль не вводят.
Спецсимволы пароля не ломают адрес; стрела без камеры и выключенная стрела в запись не попадают."""

from __future__ import annotations

import sys
from urllib.parse import unquote, urlsplit
from uuid import UUID

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from parking.runtime.container import Container
from tests.conftest import BACKEND, CAM_A1_IN, LOT_A1
from tests.support.api_fixtures import Principal, bearer

sys.path.insert(0, str(BACKEND.parent / "deploy" / "local" / "stand"))
import camera_streams

REASON = {"reason": "видеозапись камер на диск сервера"}
PASSWORD = "Cam:p@ss/w#rd 1%"


def test_rtsp_url_escapes_credentials() -> None:
    url = camera_streams.rtsp_url("http://10.10.1.64?channel=1&pulse_ms=350", "admin", PASSWORD)
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.port, parts.path) == (
        "rtsp",
        "10.10.1.64",
        554,
        "/Streaming/Channels/101",
    )
    assert unquote(parts.username or "") == "admin" and unquote(parts.password or "") == PASSWORD
    assert camera_streams.rtsp_url("10.10.1.64:8000", "admin", "x", "102").endswith(
        "@10.10.1.64:554/Streaming/Channels/102"
    )


async def test_streams_take_camera_credentials_from_its_barrier(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    base = {"lot_id": str(LOT_A1), "driver": "HIKVISION_ISAPI", "username": "admin", "password": PASSWORD, **REASON}
    linked = await client.post(
        "/api/v1/barriers",
        headers=admin,
        json={**base, "name": "Стрела въезда (запись)", "endpoint": "http://10.10.1.71", "camera_id": str(CAM_A1_IN)},
    )
    unlinked = await client.post(
        "/api/v1/barriers", headers=admin, json={**base, "name": "Стрела без камеры", "endpoint": "http://10.10.1.72"}
    )
    assert linked.status_code == 201 and unlinked.status_code == 201, (linked.text, unlinked.text)

    found = {s.barrier_id: s for s in await camera_streams.streams(container, owner)}

    stream = found[UUID(linked.json()["id"])]
    parts = urlsplit(stream.url)
    assert parts.hostname == "10.10.1.71" and unquote(parts.password or "") == PASSWORD
    assert stream.folder.startswith("cam-entry") and stream.camera_code
    assert UUID(unlinked.json()["id"]) not in found  # неизвестно, какая это камера, — писать некуда

    off = await client.patch(f"/api/v1/barriers/{linked.json()['id']}", headers=admin, json={"active": False, **REASON})
    assert off.status_code == 200, off.text
    assert UUID(linked.json()["id"]) not in {s.barrier_id for s in await camera_streams.streams(container, owner)}
