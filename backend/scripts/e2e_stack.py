"""Стенд для E2E (Playwright) и Lighthouse: отдельная БД parking_e2e (миграции + сид + пользователи + ключи камер),
API (uvicorn :8010), outbox-publisher и stream-consumer, всё с одними ключами tenant. Никаких моков: реальные
PostgreSQL/Redis, реальные саги; барьеров у площадки A2 нет → команда SKIPPED и сессия закрывается сразу.

    uv run python scripts/e2e_stack.py start   # пишет frontend/e2e/.stack.json и pid-файлы
    uv run python scripts/e2e_stack.py stop
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from uuid import UUID

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
STATE = ROOT / "frontend" / "e2e" / ".stack.json"
RUN_DIR = BACKEND / ".e2e"
DB = "parking_e2e"
API_PORT = 8010
BARRIER_PORT = 8099
RELAY_PORT = 1502
RELAY_HTTP_PORT = 8098
BARRIER_A1_IN = UUID("00000000-0000-4000-8000-00000000e101")
LOT_A1 = UUID("00000000-0000-4000-8000-00000000a101")
REDIS_URL = "redis://127.0.0.1:6390/2"
WORKER_METRICS_PORTS = {"publisher": 9464, "consumer": 9465, "replayer": 9466, "webhooks": 9467}
PASSWORD = "correct-horse-battery-staple"  # noqa: S105 — тестовый стенд, не секрет
TENANT_A = UUID("00000000-0000-4000-8000-0000000000a1")
TENANT_B = UUID("00000000-0000-4000-8000-0000000000b1")
USER_A_ADMIN = UUID("00000000-0000-4000-8000-0000000a0001")
USER_B_ADMIN = UUID("00000000-0000-4000-8000-0000000b0001")
LOT_A2 = UUID("00000000-0000-4000-8000-00000000a102")
CAM_A2_IN = UUID("00000000-0000-4000-8000-00000000c103")
CAM_A2_OUT = UUID("00000000-0000-4000-8000-00000000c104")


def _admin_dsn() -> str:
    subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "redis_dev.py"), "start", "6390"], check=True, capture_output=True
    )
    external = os.environ.get("PARKING_E2E_DSN")  # например, primary лаборатории отказов (нагрузочные прогоны)
    if external:
        return external
    subprocess.run([sys.executable, str(BACKEND / "scripts" / "pg_dev.py"), "start"], check=True, capture_output=True)
    return f"postgresql://postgres@/postgres?host={BACKEND / '.pgdata'}"


def _db_url(admin: str, dbname: str, user: str | None = None) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(admin)
    netloc = f"{user}@{parts.netloc.rpartition('@')[2]}" if user else parts.netloc
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, parts.fragment))


async def _flush_redis() -> None:
    """Стенд владеет базой 2 тестового Redis: стримы/группы/кеши прошлого прогона ссылаются на удалённую БД —
    без очистки consumer получал бы «чужие» события (NotFoundError → DLQ) и тормозил бы на их повторах."""
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL)
    try:
        await client.flushdb()
    finally:
        await client.aclose()


async def _prepare_db(admin: str) -> str:
    import asyncpg

    await _flush_redis()
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {DB}")
    finally:
        await conn.close()
    url = _db_url(admin, DB)
    env = {**os.environ, "PARKING_DATABASE_URL": url.replace("postgresql://", "postgresql+asyncpg://", 1)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        cwd=BACKEND,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise SystemExit(err.decode()[-800:])
    conn = await asyncpg.connect(url)
    try:
        await conn.execute((BACKEND / "scripts" / "seed_dev.sql").read_text(encoding="utf-8"))
    finally:
        await conn.close()
    return url


def _settings_env(url: str, keys_json: str, pepper: str, secrets_json: str) -> dict[str, str]:
    sqla = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    def role(r: str) -> str:
        return _db_url(sqla, DB, r)

    return {
        **os.environ,
        "PARKING_ENV": "dev",
        "PARKING_DATABASE_URL_CRITICAL": role("app_critical"),
        "PARKING_DATABASE_URL_BULK": role("app_bulk"),
        "PARKING_DATABASE_URL_MAINTENANCE": role("app_maintenance"),
        "PARKING_REDIS_URL": REDIS_URL,
        "PARKING_TENANT_KEYS_JSON": keys_json,
        "PARKING_CAMERA_KEY_PEPPER": pepper,
        "PARKING_SECRETS_JSON": secrets_json,
        "PARKING_CORS_ORIGINS": "[]",
        "PARKING_LOG_LEVEL": "WARNING",
        "PARKING_S3_ACCESS_KEY": "e2e",
        "PARKING_S3_SECRET_KEY": "e2e",
        "PARKING_BARRIER_DRIVERS_ENABLED": "true",
        "PARKING_REDIS_SOCKET_TIMEOUT_SECONDS": os.environ.get("PARKING_REDIS_SOCKET_TIMEOUT_SECONDS", "1.0"),
        # v2 этап 3: самостоятельная регистрация на стенде — KEK детерминированный (строки tenant_key переживают
        # перезапуск стенда), код подтверждения возвращается в ответе (SMS нет), лимит заявок с IP поднят для E2E
        "PARKING_KEY_WRAPPING_KEY": base64.b64encode(hashlib.sha256(b"anpr-e2e-stand-kek").digest()).decode(),
        "PARKING_SIGNUP_DEV_CODES": "true",
        "PARKING_SIGNUP_RATE_PER_HOUR": "1000",
    }


async def _seed_users_and_keys(env: dict[str, str]) -> dict[str, object]:
    os.environ.update(env)
    from parking.modules.identity.application.contracts import CurrentUser, Role
    from parking.modules.identity.application.user_service import CreateUserCommand
    from parking.platform.config import load_settings
    from parking.platform.db.engine import build_database
    from parking.runtime.container import build_container

    settings = load_settings()
    database = build_database(settings)
    container = build_container(settings, database)
    users: dict[str, dict[str, str]] = {}
    plan = [
        (TENANT_A, USER_A_ADMIN, Role.PLATFORM_ADMIN, "e2e.platform@alpha-parking.uz"),  # DLQ replay (runbook 7)
        (TENANT_A, USER_A_ADMIN, Role.TENANT_ADMIN, "e2e.admin@alpha-parking.uz"),
        (TENANT_A, USER_A_ADMIN, Role.MANAGER, "e2e.manager@alpha-parking.uz"),
        (TENANT_A, USER_A_ADMIN, Role.OPERATOR, "e2e.operator@alpha-parking.uz"),
        (TENANT_A, USER_A_ADMIN, Role.CASHIER, "e2e.cashier@alpha-parking.uz"),
        (TENANT_A, USER_A_ADMIN, Role.VIEWER, "e2e.viewer@alpha-parking.uz"),
        (TENANT_A, USER_A_ADMIN, Role.OWNER, "e2e.owner@alpha-parking.uz"),  # v2: владелец парковки
        (TENANT_A, USER_A_ADMIN, Role.DEVELOPER, "e2e.developer@alpha-parking.uz"),  # v2: интеграции
        (TENANT_A, USER_A_ADMIN, Role.PLATFORM_OWNER, "e2e.platform-owner@alpha-parking.uz"),  # v2: владелец платформы
        (TENANT_B, USER_B_ADMIN, Role.TENANT_ADMIN, "e2e.admin@beta-garage.uz"),
    ]
    for tenant_id, admin_id, role, email in plan:
        actor = CurrentUser(user_id=admin_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN, lot_scope=frozenset())
        user = await container.users.create(
            actor,
            CreateUserCommand(
                email=email, password=PASSWORD, role=role, display_name=email.split("@")[0], lot_scope=frozenset()
            ),
            reason="e2e stack",
        )
        users[role.value.lower() if tenant_id == TENANT_A else "tenant_b_admin"] = {
            "email": email,
            "password": PASSWORD,
            "user_id": str(user.id),
        }
    admin = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
    entry_key = await container.devices.issue_key(admin, CAM_A2_IN, label="e2e", expires_in=None, reason="e2e stack")
    exit_key = await container.devices.issue_key(admin, CAM_A2_OUT, label="e2e", expires_in=None, reason="e2e stack")
    barrier_url = None
    ip = host_ip()
    if ip is not None:
        import asyncpg

        from parking.modules.devices.application.admin import CreateBarrierCommand
        from parking.modules.devices.domain.barrier import BarrierDriverKind

        conn = await asyncpg.connect(
            env["PARKING_DATABASE_URL_CRITICAL"]
            .replace("postgresql+asyncpg://", "postgresql://", 1)
            .replace("app_critical@", "postgres@")
        )
        try:
            await conn.execute("UPDATE parking_lot SET barrier_cidr_allowlist = $1 WHERE id = $2", [f"{ip}/32"], LOT_A2)
            await conn.execute(  # шлагбаум сида «въезд 1» ТЦ Mega → имитатор реле (MODBUS_TCP, импульс 0,5 с)
                "UPDATE barrier SET endpoint = $1 WHERE id = $2",
                f"{ip}:{RELAY_PORT}?unit=1&open_coil=0&pulse_ms=500",
                BARRIER_A1_IN,
            )
            await conn.execute(
                "UPDATE parking_lot SET barrier_cidr_allowlist = barrier_cidr_allowlist || $1::cidr[] WHERE id = $2",
                [f"{ip}/32"],
                LOT_A1,
            )
        finally:
            await conn.close()
        barrier_url = f"http://{ip}:{BARRIER_PORT}"
        for camera, name in ((CAM_A2_IN, "E2E въезд"), (CAM_A2_OUT, "E2E выезд")):
            await container.devices.create_barrier(
                admin,
                CreateBarrierCommand(
                    lot_id=LOT_A2,
                    name=name,
                    driver=BarrierDriverKind.HTTP_REST,
                    endpoint=barrier_url,
                    camera_id=camera,
                    auth_secret_ref="kv/tenants/alpha/barriers/e2e",  # noqa: S106 — ссылка на секрет, не секрет,
                ),
                reason="e2e stack",
            )
    await container.aclose()
    await database.dispose()
    return {
        "api": f"http://127.0.0.1:{API_PORT}",
        "users": users,
        "camera_keys": {"entry": entry_key.raw, "exit": exit_key.raw},
        "lot_id": str(LOT_A2),
        "tenant_a": str(TENANT_A),
        "barrier_controller": barrier_url,
        "relay_simulator": f"http://{ip}:{RELAY_HTTP_PORT}/" if ip else None,
        "metrics": {name: f"http://127.0.0.1:{port}/metrics" for name, port in WORKER_METRICS_PORTS.items()},
        "db_url": env["PARKING_DATABASE_URL_MAINTENANCE"].replace("postgresql+asyncpg://", "postgresql://", 1),
    }


def host_ip() -> str | None:
    """Не-loopback адрес хоста для фейкового контроллера (SSRF-политика блокирует 127.0.0.0/8)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            ip = str(probe.getsockname()[0])
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def _jwt_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def _spawn(name: str, args: list[str], env: dict[str, str]) -> None:
    RUN_DIR.mkdir(exist_ok=True)
    log = open(RUN_DIR / f"{name}.log", "wb")
    proc = subprocess.Popen(args, cwd=BACKEND, env=env, stdout=log, stderr=log, start_new_session=True)
    (RUN_DIR / f"{name}.pid").write_text(str(proc.pid))


def start() -> None:
    from parking.platform.crypto import EnvKeyProvider

    if any(RUN_DIR.glob("*.pid")):
        stop()
    admin = _admin_dsn()
    url = asyncio.run(_prepare_db(admin))
    keys_json = json.dumps(
        {str(TENANT_A): EnvKeyProvider.generate_entry(), str(TENANT_B): EnvKeyProvider.generate_entry()}
    )
    pepper = os.urandom(32).hex()
    # несколько uvicorn-воркеров: ключи JWT задаём явно (dev-ephemeral ключи различались бы между процессами)
    private_pem, public_pem = _jwt_keypair()
    os.environ["PARKING_JWT_PRIVATE_KEY"], os.environ["PARKING_JWT_PUBLIC_KEY"] = private_pem, public_pem
    env = _settings_env(
        url,
        keys_json,
        pepper,
        json.dumps({"kv/tenants/alpha/barriers/e102": "e2e-secret", "kv/tenants/alpha/barriers/e2e": "e2e-secret"}),
    )
    python = sys.executable
    ip = host_ip()
    if ip is not None:  # фейковый контроллер шлагбаума — до сидирования (валидация endpoint резолвит адрес)
        _spawn("barrier", [python, str(BACKEND / "scripts" / "fake_barrier.py"), ip, str(BARRIER_PORT)], env)
        # v2: имитатор Modbus-реле со шлагбаумом (как у пилотной площадки: APOLLO + сетевое реле) — въезд ТЦ Mega
        _spawn(
            "relay",
            [
                python,
                str(BACKEND / "scripts" / "fake_modbus_relay.py"),
                "--host",
                ip,
                "--port",
                str(RELAY_PORT),
                "--http-port",
                str(RELAY_HTTP_PORT),
            ],
            env,
        )
    state = asyncio.run(_seed_users_and_keys(env))
    if os.environ.get("E2E_DEMO_WEEK") == "1":  # v2 этап 7: неделя демо-данных для дашборда (скрипт demo_week.py)
        sys.path.insert(0, str(BACKEND / "scripts"))
        import demo_week

        print("demo week:", asyncio.run(demo_week.seed()))
    profiler = (
        [
            "uvx",
            "py-spy",
            "record",
            "--subprocesses",
            "--nonblocking",  # без остановки процесса на каждую выборку: ptrace-паузы искажали латентность в разы
            "--rate",
            os.environ.get("PARKING_E2E_PROFILE_RATE", "50"),
            "--format",
            "speedscope",
            "-o",
            str(RUN_DIR / "api-profile.speedscope.json"),
            "--",
        ]
        if os.environ.get("PARKING_E2E_PROFILE") == "1"
        else []
    )
    _spawn(
        "api",
        [
            *profiler,
            python,
            "-m",
            "uvicorn",
            "parking.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
            "--workers",
            os.environ.get("PARKING_E2E_WORKERS", "1"),
            "--timeout-keep-alive",
            "75",  # дольше простоя клиента (k6/ingress): иначе гонка закрытия keep-alive → «connection reset» у камер
            "--log-level",
            "warning",
        ],
        env,
    )
    # у каждого воркера свой /metrics (WORKER_METRICS_PORTS): SLI выезда end-to-end наблюдается в consumer'е
    workers = {
        "publisher": "from parking.workers.runtime import main_publisher; main_publisher()",
        "consumer": "from parking.workers.runtime import main_consumer; main_consumer()",
        "replayer": "from parking.workers.runtime import main_offline_replayer; main_offline_replayer()",
        "webhooks": "from parking.workers.runtime import main_webhooks; main_webhooks()",
    }
    for name, entry in workers.items():
        _spawn(name, [python, "-c", entry], {**env, "PARKING_METRICS_PORT": str(WORKER_METRICS_PORTS[name])})
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/health/live", timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.3)
    else:
        raise SystemExit("api did not start; see backend/.e2e/api.log")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    print(json.dumps({"api": state["api"], "lot_id": state["lot_id"]}))


PATTERNS = (
    "uvicorn parking.api.main:app --host 127.0.0.1 --port " + str(API_PORT),
    "scripts/fake_barrier.py",
    "scripts/fake_modbus_relay.py",
    "from parking.workers.runtime import main_publisher",
    "from parking.workers.runtime import main_consumer",
    "from parking.workers.runtime import main_offline_replayer",
    "from parking.workers.runtime import main_webhooks",
)


def _stale_pids() -> list[int]:
    """Процессы стенда по командной строке (pid-файлы могли потеряться): без них следующий start уронит БД под живым процессом."""
    out = subprocess.run(["pgrep", "-f", "|".join(PATTERNS)], capture_output=True, text=True, check=False).stdout
    found: list[int] = []
    for raw in out.split():
        pid = int(raw)
        if pid == os.getpid():
            continue
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].decode(errors="replace") == sys.executable:  # только наши python-процессы, не оболочка
            found.append(pid)
    return found


def _is_profiler(pid: int) -> bool:
    try:
        return b"py-spy" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def stop() -> None:
    pids: set[int] = set(_stale_pids())
    for pid_file in RUN_DIR.glob("*.pid"):
        try:
            pids.add(int(pid_file.read_text()))
        except ValueError:
            pass
        pid_file.unlink()
    for pid in sorted(pids, key=lambda p: not _is_profiler(p)):  # py-spy первым: SIGINT → он дописывает профиль
        if _is_profiler(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGINT)
                for _ in range(300):
                    os.kill(pid, 0)
                    time.sleep(0.1)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            for _ in range(30):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                continue
            break
    if STATE.exists():
        STATE.unlink()
    print("stopped")


if __name__ == "__main__":
    sys.path.insert(0, str(BACKEND / "src"))
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command == "start":
        start()
    elif command == "stop":
        stop()
    else:
        print(__doc__)
        raise SystemExit(1)
