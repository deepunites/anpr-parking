"""Локальный MinIO для разработки/тестов.

uv run python scripts/minio_dev.py start [port]   # 9000; данные — backend/.minio-data; креды из .env.dev (генерируются)
uv run python scripts/minio_dev.py stop  [port]
uv run python scripts/minio_dev.py setup [port]   # бакет + lifecycle 90 дней + проверка `mc ilm ls`
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BIN = BACKEND / ".minio" / "bin"
DATA = BACKEND / ".minio-data"
ENV_FILE = BACKEND / ".env.dev"
BUCKET = "anpr-media"


def credentials() -> tuple[str, str]:
    access, secret = os.environ.get("PARKING_S3_ACCESS_KEY"), os.environ.get("PARKING_S3_SECRET_KEY")
    if access and secret:  # CI: ключи из окружения — одни и те же у MinIO, mc и настроек тестов, без гонки за .env.dev
        return access, secret
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    changed = False
    if "PARKING_S3_ACCESS_KEY" not in env:
        env["PARKING_S3_ACCESS_KEY"], changed = "dev" + secrets.token_hex(6), True
    if "PARKING_S3_SECRET_KEY" not in env:
        env["PARKING_S3_SECRET_KEY"], changed = secrets.token_urlsafe(24), True
    if changed:
        ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    return env["PARKING_S3_ACCESS_KEY"], env["PARKING_S3_SECRET_KEY"]


def alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/minio/health/live", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def start(port: int) -> None:
    if not (BIN / "minio").exists():
        subprocess.run([sys.executable, str(BACKEND / "scripts" / "fetch_minio.py")], check=True)
    if alive(port):
        return
    access, secret = credentials()
    DATA.mkdir(exist_ok=True)
    log = open(DATA / f"minio-{port}.log", "ab")
    subprocess.Popen(
        [
            str(BIN / "minio"),
            "server",
            str(DATA / "store"),
            "--address",
            f"127.0.0.1:{port}",
            "--console-address",
            f"127.0.0.1:{port + 1}",
            "--quiet",
        ],
        env={**os.environ, "MINIO_ROOT_USER": access, "MINIO_ROOT_PASSWORD": secret, "MINIO_BROWSER": "off"},
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    for _ in range(100):
        if alive(port):
            return
        time.sleep(0.2)
    raise SystemExit("minio did not start")


def mc(port: int, *args: str) -> subprocess.CompletedProcess[str]:
    access, secret = credentials()
    env = {**os.environ, "MC_HOST_local": f"http://{access}:{secret}@127.0.0.1:{port}"}
    return subprocess.run([str(BIN / "mc"), *args], env=env, capture_output=True, text=True, check=False)


def setup(port: int, bucket: str = BUCKET) -> str:
    """Бакет, versioning, lifecycle 90 дней (фаза 2 §4 / фаза 4 §5). Возвращает вывод `mc ilm ls` — доказательство, что policy применена."""
    mc(port, "mb", "--ignore-existing", f"local/{bucket}")
    mc(port, "version", "enable", f"local/{bucket}")
    existing = mc(port, "ilm", "rule", "ls", f"local/{bucket}", "--json")
    if '"Days":90' not in existing.stdout.replace(" ", ""):
        # у mc нет флага --id (на неизвестный флаг он печатает справку и ничего не создаёт) — ID правилу даёт MinIO
        added = mc(
            port, "ilm", "rule", "add", "--expire-days", "90", "--noncurrent-expire-days", "7", f"local/{bucket}"
        )
        if added.returncode != 0:
            raise SystemExit(f"mc ilm rule add: {added.stderr.strip()[-500:]}")
    return mc(port, "ilm", "rule", "ls", f"local/{bucket}").stdout


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
    if cmd == "start":
        start(port)
        print(f"http://127.0.0.1:{port}")
    elif cmd == "stop":
        subprocess.run(["pkill", "-f", f"minio server .* --address 127.0.0.1:{port}"], check=False)
    elif cmd == "setup":
        start(port)
        print(setup(port))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
