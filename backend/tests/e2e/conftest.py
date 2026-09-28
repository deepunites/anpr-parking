"""E2E на поднятом стенде (фаза 8 §4): реальные процессы API/publisher/consumer + фейковый контроллер шлагбаума
(backend/scripts/e2e_stack.py). Запуск: `uv run pytest tests/e2e -m e2e`."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[2]
STATE = BACKEND.parent / "frontend" / "e2e" / ".stack.json"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@dataclass
class Stack:
    state: dict[str, Any]

    @property
    def api(self) -> str:
        return str(self.state["api"])

    @property
    def barrier(self) -> str | None:
        return self.state.get("barrier_controller")

    def token(self, client: httpx.Client, role: str) -> dict[str, str]:
        user = self.state["users"][role]
        resp = client.post(f"{self.api}/api/v1/auth/login", json={"email": user["email"], "password": user["password"]})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def commands(self) -> list[dict[str, Any]]:
        if self.barrier is None:
            return []
        return list(httpx.get(f"{self.barrier}/commands", timeout=5).json())

    def wait(self, predicate: Any, *, limit: float = 30.0, every: float = 0.3) -> float:
        started = time.monotonic()
        while time.monotonic() - started < limit:
            if predicate():
                return time.monotonic() - started
            time.sleep(every)
        raise AssertionError(f"condition not met within {limit}s")


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    script = BACKEND / "scripts" / "e2e_stack.py"
    subprocess.run([sys.executable, str(script), "start"], check=True, capture_output=True, text=True)
    try:
        yield Stack(json.loads(STATE.read_text()))
    finally:
        subprocess.run([sys.executable, str(script), "stop"], check=False, capture_output=True)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=15) as client:
        yield client
