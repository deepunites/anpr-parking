"""Локальный Redis для разработки/тестов.

uv run python scripts/redis_dev.py start [port]   # по умолчанию 6379, данные в backend/.redis-data
uv run python scripts/redis_dev.py stop  [port]
uv run python scripts/redis_dev.py cli   [port]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BIN = BACKEND / ".redis" / "bin"
DATA = BACKEND / ".redis-data"


def _run(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, **kw)  # type: ignore[call-overload]


def ping(port: int) -> bool:
    return _run([str(BIN / "redis-cli"), "-p", str(port), "ping"]).stdout.strip() == "PONG"


def start(port: int) -> None:
    if not (BIN / "redis-server").exists():
        subprocess.run([sys.executable, str(BACKEND / "scripts" / "fetch_redis.py")], check=True)
    if ping(port):
        return
    DATA.mkdir(exist_ok=True)
    subprocess.Popen(
        [
            str(BIN / "redis-server"),
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--dir",
            str(DATA),
            "--appendonly",
            "yes",
            "--appendfsync",
            "everysec",
            "--daemonize",
            "yes",
            "--logfile",
            str(DATA / f"redis-{port}.log"),
            "--maxmemory",
            "256mb",
            "--maxmemory-policy",
            "noeviction",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if ping(port):
            return
        time.sleep(0.1)
    raise SystemExit("redis did not start")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 6379
    if cmd == "start":
        start(port)
        print(f"redis://127.0.0.1:{port}/0")
    elif cmd == "stop":
        _run([str(BIN / "redis-cli"), "-p", str(port), "shutdown", "nosave"])
    elif cmd == "cli":
        os.execv(str(BIN / "redis-cli"), [str(BIN / "redis-cli"), "-p", str(port), *sys.argv[3:]])  # noqa: S606
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
