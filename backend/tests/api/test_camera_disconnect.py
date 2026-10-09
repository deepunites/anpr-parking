"""Камера, которая отправляет событие и сразу закрывает соединение (DS-TCG406-E, cpp-httplib/0.9, ~30 мс при
выключенной «Проверке ответа платформы»): событие всё равно обработано. Настоящий uvicorn — у ASGITransport нет
обрывов, а uvicorn после закрытия отдаёт http.disconnect вместо уже полученного тела (ClientDisconnect, 500)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI

from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN
from tests.failure.support import wait_until
from tests.support.camera_fixtures import issue_camera_key, unique_plate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fake_hikvision_camera.py"
# контрольный сигнал ровно в том виде, в каком его шлёт камера (снят записывающим прокси на стенде)
HEARTBEAT_XML = (
    b'<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">\r\n'
    b"<ipAddress>192.168.1.64</ipAddress>\r\n<dateTime>2026-10-09T14:40:23.230+05:00</dateTime>\r\n"
    b"<eventType>heartBeat</eventType>\r\n<eventState>active</eventState>\r\n"
    b"<eventDescription>heartBeat</eventDescription>\r\n</EventNotificationAlert>\r\n"
)


def _sim() -> Any:
    spec = importlib.util.spec_from_file_location("fake_hikvision_camera", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _uvicorn(app: FastAPI) -> AsyncIterator[int]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        assert not task.done(), "uvicorn did not start"
        await asyncio.sleep(0.01)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        await task


async def _fire_and_forget(port: int, path: str, files: dict[str, tuple[str, bytes, str]]) -> None:
    """Как камера: запрос целиком, затем закрыть соединение, не читая ответ."""
    request = httpx.Request("POST", f"http://127.0.0.1:{port}{path}", files=files)
    body = request.read()
    head = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: */*\r\nUser-Agent: cpp-httplib/0.9\r\n"
    head += f"Content-Type: {request.headers['content-type']}\r\nContent-Length: {len(body)}\r\n\r\n"
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def test_camera_closing_early_still_delivers(
    app: FastAPI, container: Container, owner: asyncpg.Connection
) -> None:
    key = await issue_camera_key(container, CAM_A1_IN)
    path = f"/api/v1/camera/vendor/hikvision/{key}"
    started = datetime.now(tz=UTC)
    sim = _sim()
    async with _uvicorn(app) as port:
        await _fire_and_forget(port, path, {"heartBeat": ("heartBeat.xml", HEARTBEAT_XML, "application/xml")})

        async def heartbeat_recorded() -> bool:
            beat = (await container.heartbeats.read_many([CAM_A1_IN])).get(CAM_A1_IN)
            return beat is not None and beat.at >= started

        await wait_until(heartbeat_recorded, limit=5, interval=0.05)  # без EagerBodyMiddleware — сигнал потерян

        await _fire_and_forget(
            port,
            path,
            {
                "anpr.xml": ("anpr.xml", sim.anpr_xml(unique_plate()), "text/xml"),
                "licensePlatePicture.jpg": ("licensePlatePicture.jpg", sim.JPEG, "image/jpeg"),
            },
        )

        async def event_stored() -> bool:
            count = await owner.fetchval(
                "SELECT count(*) FROM anpr_event WHERE camera_id = $1 AND received_at >= $2", CAM_A1_IN, started
            )
            return bool(count)

        await wait_until(event_stored, limit=5, interval=0.05)
