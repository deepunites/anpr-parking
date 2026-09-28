"""Пакет failure-тестов (фаза 8 §5) запускается отдельно: `uv run pytest tests/failure -m failure`.
Все тесты идут через лабораторию (tests/failure/lab.py): PARKING_TEST_DSN указывает на toxiproxy → primary.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from tests.failure.lab import FailureLab
from tests.support.fakes import FakeBarrierDriver

from parking.runtime.container import Container

pytestmark = pytest.mark.failure


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/failure" in str(item.fspath):
            item.add_marker(pytest.mark.failure)


@pytest.fixture(scope="session", autouse=True)
def lab() -> Iterator[FailureLab]:
    """Стартует до test_dsn (autouse session-фикстуры инициализируются первыми) и подменяет DSN кластера."""
    started = FailureLab.start()
    os.environ["PARKING_TEST_DSN"] = started.admin_dsn
    try:
        yield started
    finally:
        started.stop()


@pytest.fixture(autouse=True)
def _fake_barrier_drivers(request: pytest.FixtureRequest) -> None:
    """Шлагбаумы стенда недостижимы (контроллеров нет): реальный драйвер ждал бы таймаут на каждое оффлайн-открытие
    и маскировал бы латентность самого API. Отказы шлагбаумов — отдельные сценарии с FakeBarrierDriver(results=[...])."""
    if "container" not in request.fixturenames:
        return
    container: Container = request.getfixturevalue("container")
    fake = FakeBarrierDriver()
    for name in ("MODBUS_TCP", "HTTP_REST"):
        container.drivers._drivers[name] = fake
