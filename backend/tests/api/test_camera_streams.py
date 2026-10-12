"""Круглосуточная запись камер на диск сервера площадки: deploy/local/stand/camera_streams.py берёт логин и пароль
камеры у её «Видеозаписи» или у её стрелы Hikvision (зашифрованы ключом клиента) и собирает RTSP-адрес потока.
Спецсимволы пароля не ломают адрес; стрела без камеры и выключенная стрела в запись не попадают."""

from __future__ import annotations

import sys
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from parking.runtime.container import Container
from tests.conftest import BACKEND, LOT_A1
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


async def _camera(client: httpx.AsyncClient, admin: dict[str, str], direction: str) -> str:
    created = await client.post(
        "/api/v1/cameras",
        headers=admin,
        json={
            "lot_id": str(LOT_A1),
            "code": f"REC_{uuid4().hex[:8]}",
            "name": "Камера записи",
            "direction": direction,
            "vendor": "HIKVISION",
            **REASON,
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _streams(container: Container, owner: asyncpg.Connection) -> dict[UUID, camera_streams.Stream]:
    return {s.camera_id: s for s in await camera_streams.streams(container, owner)}


async def test_streams_take_camera_credentials_from_its_barrier(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    entry, bare = await _camera(client, admin, "IN"), await _camera(client, admin, "IN")
    base = {"lot_id": str(LOT_A1), "driver": "HIKVISION_ISAPI", "username": "admin", "password": PASSWORD, **REASON}
    linked = await client.post(
        "/api/v1/barriers",
        headers=admin,
        json={**base, "name": "Стрела въезда (запись)", "endpoint": "http://10.10.1.71", "camera_id": entry},
    )
    unlinked = await client.post(
        "/api/v1/barriers", headers=admin, json={**base, "name": "Стрела без камеры", "endpoint": "http://10.10.1.72"}
    )
    assert linked.status_code == 201 and unlinked.status_code == 201, (linked.text, unlinked.text)

    found = await _streams(container, owner)

    stream = found[UUID(entry)]
    parts = urlsplit(stream.url)
    assert parts.hostname == "10.10.1.71" and unquote(parts.password or "") == PASSWORD
    assert stream.folder.startswith("cam-entry") and stream.camera_code.startswith("REC_")
    assert UUID(bare) not in found  # ни стрелы, ни адреса видео — писать нечем

    off = await client.patch(f"/api/v1/barriers/{linked.json()['id']}", headers=admin, json={"active": False, **REASON})
    assert off.status_code == 200, off.text
    assert UUID(entry) not in await _streams(container, owner)


async def test_camera_without_barrier_is_recorded_with_its_own_video_login(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, users: dict[Role, Principal]
) -> None:
    """Стрела одна на въезд и выезд (её открывает реле камеры въезда) — у выездной камеры своей стрелы нет:
    адрес, логин и пароль для записи задаются у самой камеры."""
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    exit_camera = await _camera(client, admin, "OUT")
    url = f"/api/v1/cameras/{exit_camera}"
    video = {"video_endpoint": "http://10.10.1.81", "video_username": "video", "video_password": PASSWORD}

    half = await client.patch(url, headers=admin, json={"video_endpoint": "http://10.10.1.81", **REASON})
    assert half.status_code == 422 and half.json()["error"]["code"] == "CREDENTIALS_REQUIRED"
    outside = await client.patch(url, headers=admin, json={**video, "video_endpoint": "http://192.168.77.81", **REASON})
    assert outside.status_code == 422  # частный адрес не из подсетей площадки — SSRF-политика, как у стрел
    saved = await client.patch(url, headers=admin, json={**video, **REASON})
    assert saved.status_code == 200, saved.text
    assert saved.json()["video_endpoint"] == "http://10.10.1.81" and saved.json()["has_video_credentials"] is True
    assert PASSWORD not in saved.text and "video_password" not in saved.json()
    stored = await owner.fetchval("SELECT video_credentials_encrypted FROM camera WHERE id = $1", UUID(exit_camera))
    assert stored is not None and PASSWORD.encode() not in bytes(stored)

    stream = (await _streams(container, owner))[UUID(exit_camera)]
    parts = urlsplit(stream.url)
    assert parts.hostname == "10.10.1.81" and unquote(parts.username or "") == "video"
    assert unquote(parts.password or "") == PASSWORD and stream.folder.startswith("cam-exit")

    # адрес нельзя сменить без пароля: иначе сохранённый пароль ушёл бы на чужой адрес
    moved = await client.patch(url, headers=admin, json={"video_endpoint": "http://10.10.1.82", **REASON})
    assert moved.status_code == 422
    cleared = await client.patch(url, headers=admin, json={"video_off": True, **REASON})
    assert cleared.status_code == 200 and cleared.json()["has_video_credentials"] is False
    assert cleared.json()["video_endpoint"] is None
    assert UUID(exit_camera) not in await _streams(container, owner)


async def test_camera_video_login_wins_over_barrier_login(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    camera = await _camera(client, admin, "IN")
    barrier = await client.post(
        "/api/v1/barriers",
        headers=admin,
        json={
            "lot_id": str(LOT_A1),
            "driver": "HIKVISION_ISAPI",
            "name": "Стрела камеры",
            "endpoint": "http://10.10.1.91",
            "camera_id": camera,
            "username": "admin",
            "password": "Barrier-Pass-1",
            **REASON,
        },
    )
    assert barrier.status_code == 201, barrier.text
    video = {"video_endpoint": "http://10.10.1.91", "video_username": "viewer", "video_password": "Viewer-Pass-2"}
    assert (await client.patch(f"/api/v1/cameras/{camera}", headers=admin, json={**video, **REASON})).status_code == 200

    parts = urlsplit((await _streams(container, owner))[UUID(camera)].url)
    assert unquote(parts.username or "") == "viewer" and unquote(parts.password or "") == "Viewer-Pass-2"
