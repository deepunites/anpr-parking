"""Фаза 9 §2: SIGTERM останавливает цикл воркера штатно (событие stop доходит до цикла), а не через SIGKILL."""

from __future__ import annotations

import asyncio
import os
import signal
import time

import pytest

from parking.runtime.container import Container
from parking.workers import runtime


async def test_sigterm_stops_consumer_publisher_replayer_and_camera_health(container: Container) -> None:
    started: list[str] = []

    async def fake_container() -> Container:
        return container

    for runner, name in (
        (runtime.run_consumer, "consumer"),
        (runtime.run_publisher, "publisher"),
        (runtime.run_offline_replayer, "replayer"),
        (runtime.run_camera_health, "camera-health"),
    ):
        stop = asyncio.Event()
        task = asyncio.create_task(runner(container, stop))  # type: ignore[arg-type]
        await asyncio.sleep(0.3)
        assert not task.done(), f"{name}: цикл должен работать до сигнала"
        started.append(name)
        t0 = time.perf_counter()
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        assert time.perf_counter() - t0 < 5, f"{name}: остановка после stop дольше 5 с"
    assert started == ["consumer", "publisher", "replayer", "camera-health"]


async def test_serve_wires_signal_to_stop(container: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """_serve: обработчик SIGTERM ставит stop, который получает runner, — процесс завершает цикл сам."""

    class _NoDispose:  # _serve закрывает пулы контейнера в finally; фикстура делает это сама
        settings = container.settings
        redis = container.redis

        class database:  # noqa: N801 — имитирует Container.database
            @staticmethod
            async def dispose() -> None:
                return None

    monkeypatch.setattr(runtime, "container", _NoDispose)
    seen: dict[str, asyncio.Event] = {}

    async def runner(c: object, stop: asyncio.Event) -> None:
        seen["stop"] = stop
        await stop.wait()

    async def send_sigterm() -> None:
        await asyncio.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(asyncio.gather(runtime._serve(runner), send_sigterm()), timeout=10)  # type: ignore[arg-type]
    assert seen["stop"].is_set(), "SIGTERM дошёл до цикла воркера через stop"


async def test_supervisor_restarts_loop_after_infrastructure_error() -> None:
    """Redis/PostgreSQL отвалились посреди цикла → цикл перезапускается с бэкоффом, процесс живёт (runbook 3)."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    attempts: list[int] = []
    slept: list[float] = []

    async def flaky(stop: asyncio.Event) -> None:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise RedisConnectionError("Error 111 connecting to 127.0.0.1:6390")
        stop.set()

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    restarts = await runtime.supervise("consumer", flaky, asyncio.Event(), sleep=fake_sleep)
    assert restarts == 2 and attempts == [0, 1, 2] and slept == [1.0, 2.0]
