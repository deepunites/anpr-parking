"""Redis HA (фаза 10 §5 «Redis HA»): master + replica + 3 Sentinel (кворум 2) на локальных бинарниках `.redis/bin`.
Учение: клиент приложения (`build_redis` с `redis+sentinel://`) пишет, master убивается SIGKILL, Sentinel продвигает
реплику, тот же клиент без пересоздания продолжает писать — замеряется RTO; данные до отказа на новом master."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ReadOnlyError
from redis.exceptions import TimeoutError as RedisTimeoutError

from parking.platform.redis import build_redis, celery_broker, parse_sentinel_url

BIN = Path(__file__).resolve().parents[2] / ".redis" / "bin"
MASTER, REPLICA = 6395, 6396
SENTINELS = (26395, 26396, 26397)
NAME = "parking"

pytestmark = pytest.mark.failure


def _spawn(args: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cli(port: int, *cmd: str) -> str:
    out = subprocess.run([str(BIN / "redis-cli"), "-p", str(port), *cmd], capture_output=True, text=True, check=False)
    return out.stdout.strip()


@pytest.fixture
async def sentinel_lab(tmp_path: Path) -> AsyncIterator[dict[str, subprocess.Popen[bytes]]]:
    if not (BIN / "redis-sentinel").exists():
        pytest.skip("нет .redis/bin (scripts/fetch_redis.py)")
    procs: dict[str, subprocess.Popen[bytes]] = {}
    for name, port, extra in (("master", MASTER, []), ("replica", REPLICA, ["--replicaof", "127.0.0.1", str(MASTER)])):
        d = tmp_path / name
        d.mkdir()
        procs[name] = _spawn(
            [
                str(BIN / "redis-server"),
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                *extra,
            ],
            d,
        )
    for port in SENTINELS:
        d = tmp_path / f"sentinel-{port}"
        d.mkdir()
        conf = d / "sentinel.conf"
        conf.write_text(
            f"port {port}\nbind 127.0.0.1\nsentinel monitor {NAME} 127.0.0.1 {MASTER} 2\n"
            f"sentinel down-after-milliseconds {NAME} 1000\nsentinel failover-timeout {NAME} 5000\n"
            f"sentinel parallel-syncs {NAME} 1\n"
        )
        procs[f"sentinel-{port}"] = _spawn([str(BIN / "redis-sentinel"), str(conf)], d)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:  # реплика догнала master и все Sentinel видят его
        if _cli(REPLICA, "info", "replication").find("master_link_status:up") >= 0 and all(
            NAME in _cli(p, "sentinel", "masters") for p in SENTINELS
        ):
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail("лаборатория Sentinel не поднялась за 15 с")
    try:
        yield procs
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
        for proc in procs.values():
            proc.wait(timeout=5)


def test_sentinel_url_parsing_and_celery_broker() -> None:
    url = "redis+sentinel://:pw%40d@s1:26379,s2:26379,s3:26379/parking?db=2&sentinel_password=sp"
    target = parse_sentinel_url(url)
    assert target is not None
    assert target.sentinels == (("s1", 26379), ("s2", 26379), ("s3", 26379))
    assert (target.master_name, target.db, target.password, target.sentinel_password) == ("parking", 2, "pw@d", "sp")
    broker, options = celery_broker(url)
    assert broker == "sentinel://:pw@d@s1:26379/2;sentinel://:pw@d@s2:26379/2;sentinel://:pw@d@s3:26379/2"
    assert options == {"master_name": "parking", "sentinel_kwargs": {"password": "sp"}}
    assert parse_sentinel_url("redis://localhost:6379/0") is None
    assert celery_broker("redis://localhost:6379/0") == ("redis://localhost:6379/0", {})
    with pytest.raises(ValueError, match="master name"):
        parse_sentinel_url("redis+sentinel://s1:26379/")


async def test_sentinel_failover_keeps_client_writing(sentinel_lab: dict[str, subprocess.Popen[bytes]]) -> None:
    url = "redis+sentinel://" + ",".join(f"127.0.0.1:{p}" for p in SENTINELS) + f"/{NAME}?db=0"
    settings = cast(Any, SimpleNamespace(redis_url=SecretStr(url)))
    client = build_redis(settings, socket_timeout=0.5)
    try:
        assert await client.set("drill:before", "1")
        assert (await client.info("server"))["tcp_port"] == MASTER
        sentinel_lab["master"].send_signal(signal.SIGKILL)
        killed = time.monotonic()
        errors = 0
        while True:
            try:
                await client.set("drill:after", "1")
                break
            except (RedisConnectionError, RedisTimeoutError, ReadOnlyError):
                errors += 1
                if time.monotonic() - killed > 30:
                    pytest.fail("Sentinel не переключил master за 30 с")
                await asyncio.sleep(0.2)
        rto = time.monotonic() - killed
        info = await client.info("server")
        assert info["tcp_port"] == REPLICA, info
        assert await client.get("drill:before") == b"1"  # реплицировано до отказа
        assert (await client.info("replication"))["role"] == "master"
        print(
            f"\nGATE redis sentinel failover: master :{MASTER} killed → new master :{REPLICA}; "
            f"same client writes again after {rto:.1f}s ({errors} failed attempts); data before failover present"
        )
        assert rto < 15
    finally:
        await client.aclose()
