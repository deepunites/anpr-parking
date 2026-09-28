"""Пробы фазы 9 §1: liveness без зависимостей; readiness = PostgreSQL + Redis, но под остаётся в Service при
недоступной БД (оффлайн-режим камер, A-60) и уходит из него при недоступном Redis."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from parking.api.v1 import health
from parking.modules.identity.application.contracts import Role
from tests.support.api_fixtures import Principal, bearer


async def test_live_and_ready_with_dependencies_up(client: httpx.AsyncClient) -> None:
    live = await client.get("/health/live")
    assert live.status_code == 200 and live.json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ok", "ready": True, "checks": {"postgresql": "ok", "redis": "ok"}}


async def test_postgres_down_is_degraded_but_still_ready(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_database: object) -> str:
        return "error: ConnectionRefusedError"

    monkeypatch.setattr(health, "_check_postgres", broken)
    ready = await client.get("/health/ready")
    assert ready.status_code == 200, "камеры продолжают принимать события в оффлайн-буфер — под не снимается"
    assert ready.json()["status"] == "degraded" and ready.json()["ready"] is True
    assert ready.json()["checks"] == {"postgresql": "error: ConnectionRefusedError", "redis": "ok"}


async def test_redis_down_makes_pod_not_ready(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(_redis: object) -> str:
        return "error: TimeoutError"

    monkeypatch.setattr(health, "_check_redis", broken)
    ready = await client.get("/health/ready")
    assert ready.status_code == 503 and ready.json()["ready"] is False
    assert ready.json()["checks"]["redis"] == "error: TimeoutError"


async def test_dependency_outage_is_503_not_500(
    app: FastAPI, container: object, monkeypatch: pytest.MonkeyPatch, users: dict[Role, Principal]
) -> None:
    """Runbook 1/3: при недоступной БД/Redis операторы получают 503 DEPENDENCY_UNAVAILABLE (с Retry-After), а не 500."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def db_down(*_: object, **__: object) -> object:
        raise ConnectionRefusedError("connection to server failed")

    async def redis_down(*_: object, **__: object) -> object:
        raise RedisConnectionError("Error 111 connecting to 127.0.0.1:6390")

    # ServerErrorMiddleware отдаёт ответ и пробрасывает исключение серверу — в тесте его не поднимаем
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await bearer(client, users[Role.TENANT_ADMIN])
        monkeypatch.setattr(container.privacy, "list", db_down)  # type: ignore[attr-defined]
        resp = await client.get("/api/v1/privacy/requests", headers=headers)
        assert resp.status_code == 503 and resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert resp.headers.get("retry-after") == "5"
        monkeypatch.setattr(container.privacy, "list", redis_down)  # type: ignore[attr-defined]
        resp = await client.get("/api/v1/privacy/requests", headers=headers)
        assert resp.status_code == 503
