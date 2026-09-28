"""Фикстуры фазы 4: ключ камеры, отправка событий, чистка оффлайн-стримов."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN, CAM_A1_OUT, TENANT_A, USER_A_ADMIN

ADMIN_A = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())


async def issue_camera_key(container: Container, camera_id: UUID = CAM_A1_IN) -> str:
    issued = await container.devices.issue_key(ADMIN_A, camera_id, label="test", expires_in=None, reason="phase-4 test")
    return issued.raw


@pytest.fixture
async def entry_key(container: Container) -> str:
    return await issue_camera_key(container, CAM_A1_IN)


@pytest.fixture
async def exit_key(container: Container) -> str:
    return await issue_camera_key(container, CAM_A1_OUT)


def event_body(
    plate: str,
    *,
    event_type: str = "ENTRY",
    camera_code: str = "ENTRY_01",
    event_time: datetime | None = None,
    confidence: int = 95,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "camera_code": camera_code,
        "event_type": event_type,
        "plate_number": plate,
        "confidence": confidence,
        "event_time": (event_time or datetime.now(tz=UTC)).isoformat(),
        "direction": "IN" if event_type == "ENTRY" else "OUT",
        "recognition": {"provider": "test", "version": "1.0"},
        **extra,
    }


async def send_event(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/api/v1/camera/events", json=body, headers={"X-Camera-Key": key})


def unique_plate(prefix: str = "01") -> str:
    return f"{prefix}{uuid4().hex[:6].upper()}"


@pytest.fixture
async def clean_streams(container: Container) -> AsyncIterator[None]:
    for key in ("anpr:offline", "anpr:dlq"):
        await container.redis.delete(key)
    yield
    for key in ("anpr:offline", "anpr:dlq"):
        await container.redis.delete(key)
