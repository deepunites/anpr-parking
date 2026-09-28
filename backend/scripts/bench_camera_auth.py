"""Замер аутентификации камеры под нагрузкой 200 RPS (gate фазы 4): p50/p95/p99 полного запроса
POST /api/v1/camera/heartbeat (auth + Redis, без PostgreSQL на горячем пути) и гистограмма camera_auth_seconds.

    PARKING_ENV=dev uv run python scripts/bench_camera_auth.py [rps] [seconds] [http://127.0.0.1:8099]

Без URL приложение исполняется в том же процессе (ASGITransport) — генератор нагрузки и API делят одно ядро,
хвост латентности завышен. С URL замеряется отдельный uvicorn (`uvicorn parking.api.main:app --workers 2`).

Требует поднятых PostgreSQL (scripts/pg_dev.py start) и Redis (scripts/redis_dev.py start); ключ выпускается
на время замера и отзывается после.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import sys
import time
from uuid import UUID

import httpx

from parking.api.app import create_app
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.platform import metrics
from parking.platform.config import load_settings
from parking.platform.db.engine import build_database
from parking.runtime.container import build_container

TENANT_A = UUID("00000000-0000-4000-8000-0000000000a1")
USER_A_ADMIN = UUID("00000000-0000-4000-8000-0000000a0001")
CAM_A1_IN = UUID("00000000-0000-4000-8000-00000000c101")


def _pct(values: list[float], p: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


async def main(rps: int, seconds: int, url: str | None) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # лог каждого запроса клиента — не часть замера
    settings = load_settings()
    database = build_database(settings)
    container = build_container(settings, database)
    app = create_app(settings, database=database)
    app.state.database, app.state.container = database, container
    admin = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
    issued = await container.devices.issue_key(admin, CAM_A1_IN, label="bench", expires_in=None, reason="auth bench")
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    transport = (
        httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=256)) if url else httpx.ASGITransport(app=app)
    )
    async with httpx.AsyncClient(transport=transport, base_url=url or "http://bench", timeout=10) as client:
        headers = {"X-Camera-Key": issued.raw}
        for _ in range(50):  # прогрев
            await client.post("/api/v1/camera/heartbeat", json={}, headers=headers)

        async def one() -> None:
            started = time.perf_counter()
            response = await client.post("/api/v1/camera/heartbeat", json={}, headers=headers)
            latencies.append((time.perf_counter() - started) * 1000)
            statuses[response.status_code] = statuses.get(response.status_code, 0) + 1

        interval = 1 / rps
        tasks: list[asyncio.Task[None]] = []
        t0 = time.perf_counter()
        for i in range(rps * seconds):
            target = t0 + i * interval
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one()))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - t0
    await container.devices.revoke_key(admin, issued.key.id, reason="auth bench done")
    mode = f"external {url}" if url else "in-process ASGI (генератор и API на одном ядре)"
    print(
        f"camera auth bench [{mode}]: target {rps} RPS × {seconds} s → {len(latencies)} requests in {elapsed:.1f} s "
        f"({len(latencies) / elapsed:.0f} RPS achieved); statuses={statuses}"
    )
    print(
        f"request latency ms: p50={_pct(latencies, 0.5):.2f} p95={_pct(latencies, 0.95):.2f} "
        f"p99={_pct(latencies, 0.99):.2f} max={max(latencies):.2f} mean={statistics.fmean(latencies):.2f}"
    )
    if not url:
        auth_samples = metrics.camera_auth_seconds.collect()[0].samples
        auth_sum = next(s.value for s in auth_samples if s.name.endswith("_sum"))
        auth_count = next(s.value for s in auth_samples if s.name.endswith("_count"))
        print(f"camera_auth_seconds: count={int(auth_count)} mean={auth_sum / auth_count * 1000:.3f} ms")
        buckets = [(s.labels["le"], int(s.value)) for s in auth_samples if s.name.endswith("_bucket")]
        print("camera_auth_seconds buckets (le → cumulative):", ", ".join(f"{le}s:{n}" for le, n in buckets))
    await container.aclose()
    await database.dispose()


if __name__ == "__main__":
    asyncio.run(
        main(
            int(sys.argv[1]) if len(sys.argv) > 1 else 200,
            int(sys.argv[2]) if len(sys.argv) > 2 else 10,
            sys.argv[3] if len(sys.argv) > 3 else None,
        )
    )
