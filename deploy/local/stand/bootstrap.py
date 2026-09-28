"""Шаг 2 стенда (одноразовый init при каждом запуске): база, миграции, демо-данные, пароли, ключи камер, бакет.

Каждый шаг идемпотентен: повторный запуск доводит стенд до нужного состояния (новые миграции после обновления кода,
недосозданные пользователи или ключи после сбоя), а уже выданные пароли и ключи не меняет. Логины и ключи камер
копятся в /state/credentials.json, человекочитаемая выжимка — /state/credentials.txt (START.bat кладёт её рядом).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import aioboto3
import asyncpg
from botocore.config import Config
from standlib import APP_ROLES, PG_DB, S3_ENDPOINT, STATE, load_secrets, stand_env, superuser_dsn

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.identity.application.user_service import CreateUserCommand
from parking.modules.identity.infrastructure.argon2_hasher import Argon2Hasher
from parking.platform.config import load_settings
from parking.platform.crypto import EnvKeyProvider, PlateCrypto
from parking.platform.db.engine import build_database
from parking.runtime.container import build_container
from parking.shared.plate import PlateNumber

APP_DIR = Path(os.environ.get("STAND_APP_DIR", "/app"))  # alembic.ini и alembic/ в образе бэкенда
SEED_DIR = Path(os.environ.get("STAND_SEED_DIR", "/seed"))  # backend/scripts: seed_dev.sql, retention.sql
CREDENTIALS = STATE / "credentials.json"
APP_UID = 10001
BUCKET = "anpr-media"

ALPHA = UUID("00000000-0000-4000-8000-0000000000a1")
BETA = UUID("00000000-0000-4000-8000-0000000000b1")
ADMINS = {ALPHA: UUID("00000000-0000-4000-8000-0000000a0001"), BETA: UUID("00000000-0000-4000-8000-0000000b0001")}
SEED_PLATES = {ALPHA: ["01B666BB", "01V001VV", "01W333WW", "01A777AA"]}  # номера правил и абонемента seed_dev.sql
DEMO_BARRIER = UUID("00000000-0000-4000-8000-00000000e101")  # «Шлагбаум въезд 1», ТЦ Mega
DEMO_LOT = UUID("00000000-0000-4000-8000-00000000a101")
RELAY_HOST = os.environ.get("STAND_RELAY_HOST", "relay-sim")
SEED_CAMERA_KEYS = ("devkey_entry_01a", "devkey_exit_01aa", "devkey_entry_01b")  # общеизвестные заглушки — отзываем
EXTRA_USERS = (  # роли v2, которых нет в seed_dev.sql
    (ALPHA, "PLATFORM_OWNER", "platform@alpha-parking.uz", "Владелец платформы"),
    (ALPHA, "OWNER", "owner@alpha-parking.uz", "Владелец парковки"),
    (ALPHA, "MANAGER", "manager@alpha-parking.uz", "Менеджер"),
)
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих 0/O, 1/l/I


def _relay_network() -> str | None:
    """Подсеть /24 имитатора реле в сети compose (SSRF-политика пускает только подсети из настроек площадки)."""
    try:
        ip = socket.gethostbyname(RELAY_HOST)
    except OSError:
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def log(msg: str) -> None:
    print(f"[init] {msg}", flush=True)


def new_password() -> str:
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


async def wait_postgres(dsn: str, limit_seconds: float = 180) -> None:
    deadline = time.monotonic() + limit_seconds
    while True:
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            await conn.close()
            return
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"PostgreSQL недоступен: {exc}") from exc
            await asyncio.sleep(2)


async def ensure_database(s: dict[str, Any]) -> None:
    conn = await asyncpg.connect(superuser_dsn(s, db="postgres"))
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", PG_DB):
            await conn.execute(f'CREATE DATABASE "{PG_DB}"')
            log(f"database {PG_DB} created")
    finally:
        await conn.close()


def migrate(s: dict[str, Any]) -> None:
    env = {**os.environ, "PARKING_DATABASE_URL": superuser_dsn(s, driver="postgresql+asyncpg")}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"миграции не прошли:\n{proc.stderr[-3000:]}")
    log("migrations: head")


async def prepare_roles_and_seed(s: dict[str, Any], creds: dict[str, Any]) -> None:

    conn = await asyncpg.connect(superuser_dsn(s))
    try:
        for role in APP_ROLES:  # роли создаёт миграция 0001 без пароля; вход по сети — только с паролем стенда
            await conn.execute(
                await conn.fetchval(
                    "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)", role, s["db_passwords"][role]
                )
            )
        if not await conn.fetchval("SELECT count(*) FROM tenant"):
            await conn.execute((SEED_DIR / "seed_dev.sql").read_text(encoding="utf-8"))
            log("demo data loaded (seed_dev.sql)")
        async with conn.transaction():
            hasher = Argon2Hasher()
            for row in await conn.fetch(
                "SELECT id, email, role::text AS role FROM app_user WHERE password_hash = 'dev-placeholder'"
            ):
                password = new_password()
                await conn.execute(
                    "UPDATE app_user SET password_hash = $1 WHERE id = $2", hasher.hash(password), row["id"]
                )
                creds["users"][row["email"]] = {"password": password, "role": row["role"]}
            crypto = PlateCrypto(EnvKeyProvider(json.dumps(s["tenant_keys"])))
            for tenant_id, plates in SEED_PLATES.items():
                for raw in plates:  # заглушки sha256 → настоящий HMAC и шифртекст ключами клиента
                    plate = PlateNumber.parse(raw)
                    old = hashlib.sha256(raw.encode()).digest()
                    new_hash = crypto.hash(tenant_id, plate)
                    enc, version = crypto.encrypt(tenant_id, plate)
                    await conn.execute(
                        "UPDATE access_rule SET plate_hash = $1, plate_encrypted = $2, enc_key_version = $3 "
                        "WHERE tenant_id = $4 AND plate_hash = $5",
                        new_hash,
                        enc,
                        version,
                        tenant_id,
                        old,
                    )
                    await conn.execute(
                        "UPDATE subscription SET plate_hash = $1 WHERE tenant_id = $2 AND plate_hash = $3",
                        new_hash,
                        tenant_id,
                        old,
                    )
            # «въезд 1» ТЦ Mega → имитатор Modbus-реле стенда (один раз, пока адрес — из сида)
            await conn.execute(
                "UPDATE barrier SET endpoint = $1, active = true WHERE id = $2 AND endpoint = '10.10.1.20:502'",
                f"{RELAY_HOST}:1502?unit=1&open_coil=0&pulse_ms=500",
                DEMO_BARRIER,
            )
            relay_net = _relay_network()
            if relay_net is not None:  # адрес контейнера в сети compose меняется — подсеть в аллоулисте площадки
                await conn.execute(
                    "UPDATE parking_lot SET barrier_cidr_allowlist = array(SELECT DISTINCT unnest("
                    "barrier_cidr_allowlist || $1::cidr[])) WHERE id = $2",
                    [relay_net],
                    DEMO_LOT,
                )
            # остальные шлагбаумы сида смотрят на несуществующие 10.10.1.x — consumer тратил бы секунды на повторы;
            # настоящие контроллеры добавляются в «Устройствах»
            await conn.execute(
                "UPDATE barrier SET active = false WHERE active AND endpoint IN ('10.10.1.20:502', 'http://10.10.1.21:8080')"
            )
            await conn.execute(
                "UPDATE camera_api_key SET active = false, revoked_at = now() "
                "WHERE active AND key_prefix = ANY($1::text[])",
                list(SEED_CAMERA_KEYS),
            )
    finally:
        await conn.close()


async def users_and_camera_keys(s: dict[str, Any], creds: dict[str, Any]) -> None:
    os.environ.update(stand_env(s))

    settings = load_settings()
    database = build_database(settings)
    container = build_container(settings, database)
    try:
        conn = await asyncpg.connect(superuser_dsn(s))
        try:
            existing = {r["email"] for r in await conn.fetch("SELECT email FROM app_user")}
            cameras = await conn.fetch(
                "SELECT c.id, c.tenant_id, c.code, c.name, l.name AS lot FROM camera c "
                "JOIN parking_lot l ON l.id = c.lot_id WHERE c.active AND c.tenant_id = ANY($1::uuid[]) "
                "ORDER BY l.name, c.code",
                list(ADMINS),
            )
        finally:
            await conn.close()
        for tenant_id, role, email, name in EXTRA_USERS:
            if email in existing:
                continue
            password = new_password()
            actor = CurrentUser(
                user_id=ADMINS[tenant_id], tenant_id=tenant_id, role=Role.PLATFORM_ADMIN, lot_scope=frozenset()
            )
            await container.users.create(
                actor,
                CreateUserCommand(email=email, password=password, role=Role(role), display_name=name),
                reason="локальный стенд",
            )
            creds["users"][email] = {"password": password, "role": role}
            log(f"user {email} created")
        for cam in cameras:
            key_id = str(cam["id"])
            if key_id in creds["camera_keys"]:
                continue
            tenant_id = cam["tenant_id"]
            actor = CurrentUser(
                user_id=ADMINS[tenant_id], tenant_id=tenant_id, role=Role.TENANT_ADMIN, lot_scope=frozenset()
            )
            issued = await container.devices.issue_key(
                actor, cam["id"], label="локальный стенд", expires_in=None, reason="локальный стенд"
            )
            creds["camera_keys"][key_id] = {
                "camera": f"{cam['lot']} · {cam['name']} ({cam['code']})",
                "key": issued.raw,
            }
            log(f"camera key issued: {cam['lot']} / {cam['code']}")
    finally:
        await container.aclose()
        await database.dispose()


async def demo_week(s: dict[str, Any]) -> None:
    """v2 этап 7: при первом запуске — неделя демо-трафика (въезды, оплаты, смены, ручные открытия), чтобы сводка
    владельца не была пустой. Отключается STAND_DEMO_WEEK=0; повторно не запускается (сессии уже есть)."""
    if os.environ.get("STAND_DEMO_WEEK", "1") == "0":
        return
    conn = await asyncpg.connect(superuser_dsn(s))
    try:
        if await conn.fetchval("SELECT count(*) FROM parking_session"):
            return
    finally:
        await conn.close()
    os.environ.update(stand_env(s))
    sys.path.insert(0, str(SEED_DIR))
    import demo_week as demo  # noqa: PLC0415 — backend/scripts/demo_week.py (том /seed)

    log(f"demo week: {await demo.seed()}")


async def ensure_bucket(s: dict[str, Any], limit_seconds: float = 120) -> None:

    session = aioboto3.Session()
    deadline = time.monotonic() + limit_seconds
    while True:
        try:
            async with session.client(
                "s3",
                endpoint_url=S3_ENDPOINT,
                aws_access_key_id=s["s3_access_key"],
                aws_secret_access_key=s["s3_secret_key"],
                region_name="uz-tashkent-1",
                config=Config(signature_version="s3v4", connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
            ) as s3:
                names = {b["Name"] for b in (await s3.list_buckets()).get("Buckets", [])}
                if BUCKET not in names:
                    await s3.create_bucket(Bucket=BUCKET)
                    await s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
                    await s3.put_bucket_lifecycle_configuration(
                        Bucket=BUCKET,
                        LifecycleConfiguration={
                            "Rules": [
                                {
                                    "ID": "expire-90d",
                                    "Status": "Enabled",
                                    "Filter": {"Prefix": ""},
                                    "Expiration": {"Days": 90},
                                    "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                                }
                            ]
                        },
                    )
                    log(f"bucket {BUCKET} created (versioning, 90 days)")
                return
        except Exception as exc:  # MinIO ещё поднимается
            if time.monotonic() > deadline:
                raise SystemExit(f"MinIO недоступен: {exc}") from exc
            await asyncio.sleep(3)


async def run_retention(s: dict[str, Any]) -> None:
    """Партиции outbox/событий на дни вперёд — без них через пару суток вставки упадут (далее — сервис maintenance)."""
    conn = await asyncpg.connect(superuser_dsn(s))
    try:
        await conn.execute((SEED_DIR / "retention.sql").read_text(encoding="utf-8"))
    finally:
        await conn.close()
    log("partitions ensured (retention.sql)")


def save_credentials(creds: dict[str, Any], *, echo: bool = False) -> None:
    CREDENTIALS.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    port = os.environ.get("STAND_HTTP_PORT", "8080")
    lines = [
        "ANPR Parking — локальный стенд",
        "=" * 60,
        f"Адрес:        http://localhost:{port}",
        f"Из сети:      http://<IP этого компьютера>:{port}",
        f"Регистрация:  http://localhost:{port}/signup  (код подтверждения показывается на экране)",
        "",
        "Входы (e-mail / пароль / роль):",
    ]
    width = max((len(e) for e in creds["users"]), default=0)
    for email, u in sorted(creds["users"].items(), key=lambda kv: (kv[1]["role"] != "PLATFORM_OWNER", kv[0])):
        lines.append(f"  {email.ljust(width)}  {u['password']}  {u['role']}")
    lines += ["", "Ключи камер (передаются в заголовке X-Camera-Key):"]
    for k in creds["camera_keys"].values():
        lines.append(f"  {k['camera']}")
        lines.append(f"    {k['key']}")
    lines += [
        "",
        f"События камер: POST http://<IP этого компьютера>:{port}/api/v1/camera/events",
        f"  Hikvision:   POST http://<IP этого компьютера>:{port}/api/v1/camera/vendor/hikvision",
        f"  Dahua:       POST http://<IP этого компьютера>:{port}/api/v1/camera/vendor/dahua",
        "Файл пересоздаётся при каждом запуске START.bat. Храните его как пароли.",
    ]
    text = "\n".join(lines) + "\n"
    out = STATE / "credentials.txt"
    out.write_text(text, encoding="utf-8")
    for path in (CREDENTIALS, out):
        os.chmod(path, 0o600)
        if os.geteuid() == 0:
            os.chown(path, APP_UID, APP_UID)  # читает контейнер api (START.bat забирает файл через него)
    if echo:
        print(text, flush=True)


async def main() -> None:
    s = load_secrets()
    creds: dict[str, Any] = {"users": {}, "camera_keys": {}}
    if CREDENTIALS.exists():
        creds.update(json.loads(CREDENTIALS.read_text(encoding="utf-8")))
    await wait_postgres(superuser_dsn(s, db="postgres"))
    await ensure_database(s)
    migrate(s)
    await prepare_roles_and_seed(s, creds)
    save_credentials(creds)  # пароли уже в базе — сохраняем сразу, до шагов, которые могут упасть
    await users_and_camera_keys(s, creds)
    await demo_week(s)
    await ensure_bucket(s)
    await run_retention(s)
    save_credentials(creds, echo=True)


if __name__ == "__main__":
    asyncio.run(main())
