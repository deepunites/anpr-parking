"""Общее для локального стенда (docker compose): где лежат секреты стенда и какое окружение PARKING_* из них следует.

Секреты генерируются один раз (keys.py) в томе /state и дальше только читаются: ключи шифрования номеров клиентов,
KEK, JWT, пароли ролей БД и MinIO. Потеря тома = потеря доступа к зашифрованным номерам, поэтому том удаляется
только вместе с базой (RESET.bat).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE = Path(os.environ.get("STAND_STATE", "/state"))
SECRETS_FILE = STATE / "secrets.json"
PROFILE_FILE = STATE / "profile"
PG_HOST = os.environ.get("STAND_PG_HOST", "postgres")
PG_PORT = int(os.environ.get("STAND_PG_PORT", "5432"))
PG_DB = os.environ.get("STAND_PG_DB", "parking")
REDIS_URL = os.environ.get("STAND_REDIS_URL", "redis://redis:6379/0")
S3_ENDPOINT = os.environ.get("STAND_S3_ENDPOINT", "http://minio:9000")
APP_ROLES = ("app_critical", "app_bulk", "app_maintenance")


def stand_profile() -> str:
    """demo — демо-клиенты для показа (по умолчанию, START.bat); site — настоящая площадка (сервер TrueNAS): без
    демо-данных и без открытой регистрации новых клиентов. Выбирается при первой настройке и хранится в томе /state."""
    try:
        return PROFILE_FILE.read_text(encoding="utf-8").strip() or "demo"
    except FileNotFoundError:
        return "demo"


def load_secrets() -> dict[str, Any]:
    return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))


def pg_dsn(user: str, password: str, *, db: str = PG_DB, driver: str = "postgresql") -> str:
    # host с «/» — unix-сокет (проверка скриптов стенда без Docker); в compose — имя сервиса
    if PG_HOST.startswith("/"):
        return f"{driver}://{user}:{password}@/{db}?host={PG_HOST}"
    return f"{driver}://{user}:{password}@{PG_HOST}:{PG_PORT}/{db}"


def superuser_dsn(s: dict[str, Any], *, db: str = PG_DB, driver: str = "postgresql") -> str:
    return pg_dsn("postgres", s["pg_superuser_password"], db=db, driver=driver)


def stand_env(s: dict[str, Any]) -> dict[str, str]:
    """Окружение процессов приложения. PARKING_ENV=dev: без Vault, код подтверждения регистрации на экране (SMS нет);
    DEBUG выключен — стенд может быть доступен из локальной сети (камеры, планшеты операторов)."""

    def role(name: str) -> str:
        return pg_dsn(name, s["db_passwords"][name], driver="postgresql+asyncpg")

    env = {
        "PARKING_ENV": "dev",
        "PARKING_DEBUG": "false",
        "PARKING_LOG_LEVEL": "INFO",
        "PARKING_DATABASE_URL_CRITICAL": role("app_critical"),
        "PARKING_DATABASE_URL_BULK": role("app_bulk"),
        "PARKING_DATABASE_URL_MAINTENANCE": role("app_maintenance"),
        "PARKING_DB_POOL_SIZE": "4",
        "PARKING_REDIS_URL": REDIS_URL,
        "PARKING_KEYS_SOURCE": "env",
        "PARKING_TENANT_KEYS_JSON": json.dumps(s["tenant_keys"]),
        "PARKING_CAMERA_KEY_PEPPER": s["camera_pepper"],
        "PARKING_KEY_WRAPPING_KEY": s["kek"],
        "PARKING_JWT_PRIVATE_KEY": s["jwt_private"],
        "PARKING_JWT_PUBLIC_KEY": s["jwt_public"],
        "PARKING_SECRETS_JSON": json.dumps({"kv/tenants/alpha/barriers/e102": s["barrier_secret"]}),
        "PARKING_S3_ENDPOINT": S3_ENDPOINT,
        "PARKING_S3_ACCESS_KEY": s["s3_access_key"],
        "PARKING_S3_SECRET_KEY": s["s3_secret_key"],
        "PARKING_CORS_ORIGINS": "[]",  # веб и API — один origin через nginx
        "PARKING_SIGNUP_DEV_CODES": "true",
    }
    if stand_profile() == "site":
        # настоящая площадка: регистрация чужих компаний из Wi-Fi парковки не нужна, коды на экране — тем более
        env.update({"PARKING_SIGNUP_ENABLED": "false", "PARKING_SIGNUP_DEV_CODES": "false"})
    return env
