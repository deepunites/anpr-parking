"""Драйвер Modbus TCP против имитатора реле со шлагбаумом (scripts/fake_modbus_relay.py): проверка связи без
движения стрелы, импульс открытия, отказы реле (рвёт связь, молчит, исключение Modbus), отказ SSRF-политики.
Подключение перенаправляется на 127.0.0.1 после проверки адреса: сама политика loopback запрещает."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from parking.modules.devices.application.ports import BarrierInfo
from parking.modules.devices.infrastructure import drivers
from parking.modules.devices.infrastructure.drivers import ModbusTcpDriver

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fake_modbus_relay.py"
RELAY_IP = "10.20.30.40"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("fake_modbus_relay", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StaticResolver:
    async def resolve(self, host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(RELAY_IP)]


@pytest.fixture
async def sim(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    relay = _load().RelaySimulator(auto_close=0.2)
    server = await asyncio.start_server(relay.handle_modbus, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_open = asyncio.open_connection

    async def redirected(host: str, _port: int, **kwargs: Any) -> Any:
        assert host == RELAY_IP  # драйвер подключается к проверенному адресу, а не к имени
        return await real_open("127.0.0.1", port, **kwargs)

    monkeypatch.setattr(drivers.asyncio, "open_connection", redirected)
    try:
        yield relay
    finally:
        server.close()  # wait_closed не ждём: «немое» соединение режима timeout висит до конца цикла событий
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and "handle_modbus" in repr(task.get_coro()):
                task.cancel()


def _barrier(allowlist: tuple[str, ...] = ("10.20.30.0/24",)) -> BarrierInfo:
    return BarrierInfo(
        id=uuid4(),
        lot_id=uuid4(),
        driver="MODBUS_TCP",
        endpoint=f"{RELAY_IP}:1502?unit=1&open_coil=0&close_coil=1&pulse_ms=50",
        auth_secret_ref=None,
        active=True,
        allowlist=allowlist,
    )


async def test_probe_reads_coils_without_moving_the_boom(sim: Any) -> None:
    result = await ModbusTcpDriver(resolver=StaticResolver()).probe(_barrier())
    assert result.ok, result.error
    assert result.detail == "modbus unit 1, coils 0-7: 00000000"
    assert sim.boom.state == "CLOSED" and sim.boom.opens == 0


async def test_open_pulse_raises_the_boom_and_releases_the_coil(sim: Any) -> None:
    driver = ModbusTcpDriver(resolver=StaticResolver())
    result = await driver.open(_barrier(), uuid4())
    assert result.ok, result.error
    assert sim.boom.opens == 1 and sim.boom.state == "OPENING"
    assert sim.coils[0] is False  # импульс: ON → OFF, катушка не залипла
    assert [e["event"] for e in sim.journal] == ["OPEN"]
    closed = await driver.close(_barrier(), uuid4())
    assert closed.ok and sim.state()["boom"] == "CLOSING"


@pytest.mark.parametrize(("mode", "error"), [("refuse", "modbus error"), ("exception", "exception code 4")])
async def test_relay_failures_are_reported(sim: Any, mode: str, error: str) -> None:
    sim.mode = mode
    result = await ModbusTcpDriver(resolver=StaticResolver()).open(_barrier(), uuid4())
    assert not result.ok and error in (result.error or "")
    assert sim.boom.opens == 0


async def test_silent_relay_times_out(sim: Any) -> None:
    sim.mode = "timeout"
    result = await ModbusTcpDriver(resolver=StaticResolver(), timeout=0.3).probe(_barrier())
    assert not result.ok and "modbus error" in (result.error or "")


async def test_address_outside_lot_allowlist_is_rejected(sim: Any) -> None:
    result = await ModbusTcpDriver(resolver=StaticResolver()).probe(_barrier(allowlist=("192.168.0.0/24",)))
    assert not result.ok and "endpoint rejected" in (result.error or "")
    assert sim.requests == 0
