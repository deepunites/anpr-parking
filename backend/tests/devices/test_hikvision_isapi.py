"""Шлагбаум через реле самой камеры Hikvision (DS-TCG406-E) — драйвер HIKVISION_ISAPI против имитатора камеры
(scripts/fake_hikvision_camera.py): Digest-авторизация, проверка связи без движения стрелы, открытие/закрытие через
/ISAPI/Parking/.../barrierGate, режим выхода тревоги, неверный пароль, отказ камеры, SSRF и разбор адреса.
Запрос уходит на проверенный IP камеры; транспорт теста перенаправляет его на имитатор на 127.0.0.1."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from parking.modules.devices.application.ports import BarrierInfo
from parking.modules.devices.domain.endpoint import parse_endpoint
from parking.modules.devices.infrastructure.drivers import HikvisionIsapiDriver
from parking.shared.errors import ValidationError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fake_hikvision_camera.py"
CAMERA_IP = "192.168.1.64"
TENANT = UUID("00000000-0000-4000-8000-0000000000a1")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("fake_hikvision_camera", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StaticResolver:
    async def resolve(self, host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(CAMERA_IP)]


class PlainBox:
    """Шифрование в тесте не проверяется (см. tests/platform), важно назначение: barrier:<id>."""

    def seal(self, tenant_id: UUID, purpose: str, secret: bytes) -> bytes:
        return json.dumps({"t": str(tenant_id), "p": purpose, "s": secret.decode()}).encode()

    def open(self, tenant_id: UUID, purpose: str, blob: bytes) -> bytes:
        data = json.loads(blob)
        if data["t"] != str(tenant_id) or data["p"] != purpose:
            raise ValueError("wrong purpose")
        return str(data["s"]).encode()


class ToSimulator(httpx.AsyncBaseTransport):
    def __init__(self, port: int) -> None:
        self._port = port
        self._inner = httpx.AsyncHTTPTransport()
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == CAMERA_IP  # драйвер ходит на проверенный адрес, не на имя
        self.hosts.append(request.headers["host"])
        body = await request.aread()
        forwarded = httpx.Request(
            request.method,
            request.url.copy_with(host="127.0.0.1", port=self._port),
            headers=request.headers,
            content=body,
        )
        return await self._inner.handle_async_request(forwarded)


@pytest.fixture
async def camera() -> AsyncIterator[Any]:
    sim = _load().HikvisionCameraSimulator(user="admin", password="Camera-Pass-1")
    await sim.start()
    try:
        yield sim
    finally:
        await sim.stop()


def _driver(camera: Any) -> tuple[HikvisionIsapiDriver, ToSimulator]:
    async def no_sleep(_: float) -> None:
        return None

    driver = HikvisionIsapiDriver(resolver=StaticResolver(), box=PlainBox(), timeout=2.0, sleep=no_sleep)
    transport = ToSimulator(camera.port)
    driver._client = httpx.AsyncClient(transport=transport, timeout=2.0)
    return driver, transport


def _barrier(endpoint: str = "http://cam-entry.local", password: str = "Camera-Pass-1", **kw: Any) -> BarrierInfo:
    barrier_id = uuid4()
    secret = json.dumps({"username": "admin", "password": password}).encode()
    return BarrierInfo(
        id=barrier_id,
        lot_id=uuid4(),
        driver="HIKVISION_ISAPI",
        endpoint=endpoint,
        auth_secret_ref=None,
        active=True,
        allowlist=kw.get("allowlist", ("192.168.1.0/24",)),
        tenant_id=TENANT,
        credentials_encrypted=PlainBox().seal(TENANT, f"barrier:{barrier_id}", secret),
    )


async def test_probe_reads_device_info_without_moving_the_boom(camera: Any) -> None:
    driver, transport = _driver(camera)
    result = await driver.probe(_barrier())
    assert result.ok, result.error
    assert "DS-TCG406-E" in (result.detail or "") and "V5.6.12" in (result.detail or "")
    assert camera.state.opens == 0 and camera.state.boom == "CLOSED"
    assert transport.hosts and transport.hosts[0] == "cam-entry.local"  # Host — исходное имя камеры


async def test_open_and_close_through_camera_relay(camera: Any) -> None:
    driver, _ = _driver(camera)
    barrier = _barrier()
    opened = await driver.open(barrier, uuid4())
    assert opened.ok, opened.error
    assert camera.state.opens == 1 and camera.state.boom == "OPEN"
    closed = await driver.close(barrier, uuid4())
    assert closed.ok and camera.state.boom == "CLOSED"
    assert any("barrierGate open (channel 1)" in line for line in camera.state.log)


async def test_io_mode_pulses_the_alarm_output(camera: Any) -> None:
    driver, _ = _driver(camera)
    barrier = _barrier("http://cam-entry.local?mode=io&output=1&pulse_ms=300")
    result = await driver.open(barrier, uuid4())
    assert result.ok, result.error
    assert camera.state.io_pulses == 1 and camera.state.output == "low"  # импульс: high → low
    assert (await driver.close(barrier, uuid4())).ok  # «закрыть» в режиме io делает сам контроллер стрелы


async def test_wrong_password_refusal_and_unreachable_camera(camera: Any) -> None:
    driver, _ = _driver(camera)
    bad = await driver.open(_barrier(password="wrong-password"), uuid4())
    assert not bad.ok and bad.error == "camera rejected login or password"
    assert camera.state.opens == 0
    camera.state.mode = "refuse"
    refused = await driver.open(_barrier(), uuid4())
    assert not refused.ok and "notSupport" in (refused.error or "")
    camera.state.mode = "ok"
    await camera.stop()
    down = await driver.probe(_barrier())
    assert not down.ok and "unreachable" in (down.error or "")


async def test_ssrf_policy_and_missing_credentials(camera: Any) -> None:
    driver, _ = _driver(camera)
    outside = await driver.open(_barrier(allowlist=()), uuid4())  # частный адрес камеры не в подсетях площадки
    assert not outside.ok and "endpoint rejected" in (outside.error or "")
    no_creds = BarrierInfo(
        id=uuid4(),
        lot_id=uuid4(),
        driver="HIKVISION_ISAPI",
        endpoint="http://cam",
        auth_secret_ref=None,
        active=True,
        allowlist=("192.168.1.0/24",),
        tenant_id=TENANT,
    )
    assert (await driver.open(no_creds, uuid4())).error == "camera login and password are not set"
    stolen = replace(_barrier(), id=uuid4())
    # шифротекст другого шлагбаума не подходит: назначение barrier:<id> входит в AAD
    assert (await driver.open(stolen, uuid4())).error == "camera credentials are unavailable"


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("http://192.168.1.64", True),
        ("192.168.1.64:8080", True),
        ("http://cam.local?channel=2", True),
        ("https://cam.local", False),  # самоподписанный сертификат камеры — только http
        ("http://cam.local:99999", False),  # порт вне диапазона — 422, не 500
        ("http://[fe80::1", False),
        ("http://cam.local?output=²", False),
        ("http://cam.local?mode=io&output=2&pulse_ms=800", True),
        ("http://admin:pass@cam.local", False),
        ("http://cam.local/ISAPI", False),
        ("http://cam.local?mode=relay", False),
        ("http://cam.local?pulse_ms=99999", False),
        ("ftp://cam.local", False),
    ],
)
def test_endpoint_parsing(endpoint: str, ok: bool) -> None:
    if ok:
        parsed = parse_endpoint(endpoint, driver="HIKVISION_ISAPI")
        assert parsed.scheme == "http" and parsed.port in {80, 8080}
    else:
        with pytest.raises(ValidationError):
            parse_endpoint(endpoint, driver="HIKVISION_ISAPI")


async def test_io_output_is_reset_even_if_the_pulse_is_cancelled(camera: Any) -> None:
    """Отмена паузы между high и low (выключение воркера) не оставляет выход тревоги замкнутым, а стрелу — открытой."""
    import asyncio

    async def cancelled(_: float) -> None:
        raise asyncio.CancelledError

    driver = HikvisionIsapiDriver(resolver=StaticResolver(), box=PlainBox(), timeout=2.0, sleep=cancelled)
    driver._client = httpx.AsyncClient(transport=ToSimulator(camera.port), timeout=2.0)
    with pytest.raises(asyncio.CancelledError):
        await driver.open(_barrier("http://cam-entry.local?mode=io&output=1&pulse_ms=300"), uuid4())
    assert camera.state.io_pulses == 1 and camera.state.output == "low"
