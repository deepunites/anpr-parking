"""v2 этап 8: нагрузочная проверка журнала событий камер на стенде e2e_stack.

Поток событий камер (часть — отклоняемые LOW_CONFIDENCE) с нескольких ключей (лимит 120/мин на ключ), затем сверка:
каждый запрос камеры попал в `event_journal` (буфер процесса не потерял строки), латентность приёма, задержка записи.

    uv run python scripts/journal_load.py --rps 30 --seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx

STACK = Path(__file__).resolve().parents[2] / "frontend" / "e2e" / ".stack.json"
LETTERS = "ABCDEFGHJKLMNPQRSTUVXYZ"


def answered(statuses: dict[int | str, int]) -> int:
    """Запросы, на которые API ответил (любой HTTP-код): каждый должен быть в журнале."""
    return sum(n for k, n in statuses.items() if isinstance(k, int))


def _plate(rng: random.Random) -> str:
    return f"{rng.choice(['01', '10', '30', '40'])}{rng.choice(LETTERS)}{rng.randint(100, 999)}{rng.choice(LETTERS)}{rng.choice(LETTERS)}"


async def _keys(client: httpx.AsyncClient, token: str, per_camera: int) -> list[tuple[str, str]]:
    auth = {"Authorization": f"Bearer {token}"}
    cams = (await client.get("/api/v1/cameras", headers=auth)).json()
    out = []
    for cam in (c for c in cams if c["direction"] == "IN" and c["active"]):
        for _ in range(per_camera):
            r = await client.post(
                f"/api/v1/cameras/{cam['id']}/keys", headers=auth, json={"label": "journal-load", "reason": "нагрузка"}
            )
            r.raise_for_status()
            out.append((r.json()["key"], cam["code"]))
    return out


async def run(rps: int, seconds: int, reject_share: float) -> dict[str, object]:
    stack = json.loads(STACK.read_text())
    rng = random.Random(8)  # noqa: S311 — нагрузочные данные
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=stack["api"], timeout=httpx.Timeout(60, connect=30), limits=limits) as client:
        admin = stack["users"]["tenant_admin"]
        login = await client.post("/api/v1/auth/login", json={"email": admin["email"], "password": admin["password"]})
        token = login.json()["access_token"]
        keys = await _keys(client, token, per_camera=max(1, rps // 3))
        started = datetime.now(tz=UTC)
        lat: dict[str, list[float]] = {"accepted": [], "rejected": []}
        statuses: dict[int | str, int] = {}

        async def one(key: str, code: str, reject: bool) -> None:
            body = {
                "camera_code": code,
                "event_type": "ENTRY",
                "plate_number": _plate(rng),
                "confidence": 5 if reject else 95,
                "event_time": datetime.now(tz=UTC).isoformat(),
                "direction": "IN",
                "recognition": {"provider": "load", "version": "1"},
            }
            t0 = time.perf_counter()
            try:
                r = await client.post("/api/v1/camera/events", json=body, headers={"X-Camera-Key": key})
            except httpx.HTTPError as exc:  # запрос не дошёл до API — в журнал он и не мог попасть
                statuses[type(exc).__name__] = statuses.get(type(exc).__name__, 0) + 1
                return
            ms = (time.perf_counter() - t0) * 1000
            statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
            lat["rejected" if reject else "accepted"].append(ms)

        tasks = []
        t_start = time.perf_counter()
        total = rps * seconds
        for i in range(total):
            key, code = keys[i % len(keys)]
            tasks.append(asyncio.create_task(one(key, code, rng.random() < reject_share)))
            await asyncio.sleep(max(0.0, t_start + (i + 1) / rps - time.perf_counter()))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - t_start
        finished = datetime.now(tz=UTC)

    conn = await asyncpg.connect(stack["db_url"])
    try:
        written = 0
        waited = 0.0
        for _ in range(30):  # буфер сбрасывается раз в 1 с — ждём, пока все строки лягут
            written = int(
                await conn.fetchval(
                    "SELECT count(*) FROM event_journal WHERE received_at >= $1 AND source = 'camera'", started
                )
            )
            if written >= answered(statuses):
                break
            await asyncio.sleep(0.5)
            waited += 0.5
        by_outcome = dict(
            await conn.fetch(
                "SELECT outcome, count(*) FROM event_journal WHERE received_at >= $1 AND source = 'camera' GROUP BY 1",
                started,
            )
        )
    finally:
        await conn.close()

    def pct(values: list[float], q: float) -> float:
        return round(statistics.quantiles(values, n=100)[q - 1], 1) if len(values) >= 2 else 0.0

    return {
        "target_rps": rps,
        "achieved_rps": round(total / elapsed, 1),
        "requests": total,
        "keys": len(keys),
        "statuses": statuses,
        "accepted_ms": {
            "p50": pct(lat["accepted"], 50),
            "p95": pct(lat["accepted"], 95),
            "p99": pct(lat["accepted"], 99),
        },
        "rejected_ms": {"p50": pct(lat["rejected"], 50), "p95": pct(lat["rejected"], 95)},
        "journal_rows": written,
        "journal_by_outcome": by_outcome,
        "answered": answered(statuses),
        "journal_lost": answered(statuses) - written,
        "journal_settle_seconds_after_load": waited,
        "window": [started.isoformat(), finished.isoformat()],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rps", type=int, default=30)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--reject-share", type=float, default=0.3)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.rps, args.seconds, args.reject_share)), ensure_ascii=False, indent=2))
