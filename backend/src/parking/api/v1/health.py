"""/health/live — процесс жив; /health/ready — зависимости доступны (фаза 9 §1).

Readiness проверяет PostgreSQL и Redis, но снимает под с трафика только при недоступном Redis: без Redis нет
аутентификации камер, идемпотентности и оффлайн-буфера; без PostgreSQL API продолжает принимать события камер в
оффлайн-буфер (фаза 4, breaker БД) и честно отвечает 503 на остальное. Если бы readiness падал по БД, все поды
разом ушли бы из Service во время сбоя БД — ровно то, ради чего оффлайн-режим и существует (A-60).
Liveness зависимостей не проверяет (§1: иначе кратковременная недоступность БД перезапустит все поды).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from parking.platform.db.engine import Database, Pool

router = APIRouter(prefix="/health", tags=["health"])
PROBE_TIMEOUT_SECONDS = 2.0


@router.get("/live", include_in_schema=False)
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def _check_postgres(database: Database) -> str:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS), database.session_factory(Pool.CRITICAL)() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # readiness обязан отвечать при любой ошибке зависимости
        return f"error: {type(exc).__name__}"
    return "ok"


async def _check_redis(redis: object) -> str:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await redis.ping()  # type: ignore[attr-defined]
    except Exception as exc:
        return f"error: {type(exc).__name__}"
    return "ok"


@router.get("/ready", include_in_schema=False)
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    database: Database | None = getattr(request.app.state, "database", None)
    container = getattr(request.app.state, "container", None)
    redis = getattr(container, "redis", None)
    checks["postgresql"] = "not configured" if database is None else await _check_postgres(database)
    checks["redis"] = "not configured" if redis is None else await _check_redis(redis)
    ready_to_serve = checks["redis"] == "ok"
    degraded = any(v != "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if ready_to_serve else 503,
        content={"status": "degraded" if degraded else "ok", "ready": ready_to_serve, "checks": checks},
    )
