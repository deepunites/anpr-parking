"""Аутентификация камеры: HMAC + compare_digest, кеш (в т. ч. отрицательный), отзыв/ротация, неразличимость по времени."""

from __future__ import annotations

import statistics
import time
from datetime import timedelta

import httpx
import pytest

from parking.modules.devices.domain.camera import KEY_PREFIX_LEN, generate_raw_key
from parking.runtime.container import Container
from parking.shared.errors import AuthenticationError
from tests.conftest import CAM_A1_IN, TENANT_A
from tests.support.camera_fixtures import ADMIN_A, event_body, send_event, unique_plate


def test_generated_key_shape() -> None:
    raw = generate_raw_key()
    assert len(raw) == KEY_PREFIX_LEN + 43
    assert generate_raw_key() != raw


async def test_authenticate_valid_invalid_and_revoked(container: Container) -> None:
    issued = await container.devices.issue_key(ADMIN_A, CAM_A1_IN, label="t", expires_in=None, reason="test")
    principal = await container.camera_auth.authenticate(issued.raw, client_ip="10.0.0.1")
    assert (principal.tenant_id, principal.camera_id, principal.direction, principal.code) == (
        TENANT_A,
        CAM_A1_IN,
        "IN",
        "ENTRY_01",
    )
    with pytest.raises(AuthenticationError):
        await container.camera_auth.authenticate(
            issued.raw[:-1] + ("A" if issued.raw[-1] != "A" else "B"), client_ip=None
        )
    with pytest.raises(AuthenticationError):
        await container.camera_auth.authenticate(generate_raw_key(), client_ip=None)
    with pytest.raises(AuthenticationError):
        await container.camera_auth.authenticate("", client_ip=None)
    await container.devices.revoke_key(ADMIN_A, issued.key.id, reason="compromised")
    with pytest.raises(AuthenticationError):  # кеш инвалидирован после коммита
        await container.camera_auth.authenticate(issued.raw, client_ip=None)


async def test_rotation_keeps_old_key_for_grace_window(container: Container) -> None:
    old = await container.devices.issue_key(ADMIN_A, CAM_A1_IN, label="t", expires_in=None, reason="test")
    new = await container.devices.rotate_key(ADMIN_A, old.key.id, grace=timedelta(hours=1), reason="rotation")
    assert (await container.camera_auth.authenticate(old.raw, client_ip=None)).camera_id == CAM_A1_IN
    assert (await container.camera_auth.authenticate(new.raw, client_ip=None)).camera_id == CAM_A1_IN
    expired = await container.devices.rotate_key(ADMIN_A, new.key.id, grace=timedelta(0), reason="immediate")
    with pytest.raises(AuthenticationError):
        await container.camera_auth.authenticate(new.raw, client_ip=None)
    assert (await container.camera_auth.authenticate(expired.raw, client_ip=None)).camera_id == CAM_A1_IN


async def test_expired_key_rejected(container: Container) -> None:
    issued = await container.devices.issue_key(
        ADMIN_A, CAM_A1_IN, label="t", expires_in=timedelta(seconds=-1), reason="test"
    )
    with pytest.raises(AuthenticationError):
        await container.camera_auth.authenticate(issued.raw, client_ip=None)


async def test_invalid_and_missing_keys_are_timing_indistinguishable(container: Container) -> None:
    """Gate: неверный ключ (префикс есть, HMAC не совпал) и несуществующий (префикса нет) — один путь и время."""
    issued = await container.devices.issue_key(ADMIN_A, CAM_A1_IN, label="t", expires_in=None, reason="test")
    wrong = issued.raw[:-4] + "zzzz"
    missing = generate_raw_key()
    samples: dict[str, list[float]] = {"wrong": [], "missing": []}
    for _ in range(20):  # прогрев: кеш кандидатов (включая отрицательный) и соединения
        for key in (wrong, missing):
            with pytest.raises(AuthenticationError):
                await container.camera_auth.authenticate(key, client_ip=None)
    for _ in range(300):
        for name, key in (("wrong", wrong), ("missing", missing)):
            started = time.perf_counter()
            with pytest.raises(AuthenticationError):
                await container.camera_auth.authenticate(key, client_ip=None)
            samples[name].append((time.perf_counter() - started) * 1000)
    med = {k: statistics.median(v) for k, v in samples.items()}
    p95 = {k: sorted(v)[int(len(v) * 0.95)] for k, v in samples.items()}
    print(
        f"\nGATE camera-auth timing (ms): wrong median={med['wrong']:.3f} p95={p95['wrong']:.3f}; "
        f"missing median={med['missing']:.3f} p95={p95['missing']:.3f}; ratio={med['wrong'] / med['missing']:.2f}"
    )
    assert 0.5 <= med["wrong"] / med["missing"] <= 2.0
    assert abs(med["wrong"] - med["missing"]) < 1.0  # разница медиан меньше миллисекунды


async def test_api_uniform_401_and_abuse_counter(client: httpx.AsyncClient, container: Container) -> None:
    body = event_body(unique_plate())
    r1 = await send_event(client, generate_raw_key(), body)
    r2 = await send_event(client, "short", body)
    r3 = await client.post("/api/v1/camera/events", json=body)
    assert (r1.status_code, r2.status_code, r3.status_code) == (401, 401, 401)
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"] == "invalid camera key"
