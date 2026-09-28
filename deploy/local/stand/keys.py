"""Шаг 1 стенда: секреты создаются один раз и живут в томе /state (запускается от root до PostgreSQL и MinIO).

Повторный запуск ничего не меняет — только заново раскладывает производные файлы (пароль суперпользователя
PostgreSQL и учётка MinIO читаются их контейнерами при старте).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys

from standlib import APP_ROLES, SECRETS_FILE, STATE

from parking.platform.config import generate_ed25519_pem
from parking.platform.crypto import EnvKeyProvider

TENANTS = (
    "00000000-0000-4000-8000-0000000000a1",
    "00000000-0000-4000-8000-0000000000b1",
)  # alpha, beta из seed_dev.sql
APP_UID = 10001  # пользователь образа бэкенда


def generate() -> dict[str, object]:
    private_pem, public_pem = generate_ed25519_pem()
    return {
        "tenant_keys": {t: EnvKeyProvider.generate_entry() for t in TENANTS},
        "camera_pepper": secrets.token_hex(32),
        "kek": base64.b64encode(os.urandom(32)).decode(),
        "jwt_private": private_pem,
        "jwt_public": public_pem,
        "pg_superuser_password": secrets.token_hex(24),
        "db_passwords": {r: secrets.token_hex(24) for r in APP_ROLES},
        "s3_access_key": "parking-" + secrets.token_hex(6),
        "s3_secret_key": secrets.token_urlsafe(32),
        "barrier_secret": secrets.token_urlsafe(24),
    }


def write(path: os.PathLike[str] | str, text: str, mode: int, owner: int | None = None) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)
    if owner is not None and os.geteuid() == 0:
        os.chown(path, owner, owner)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    if SECRETS_FILE.exists():
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        print("stand keys: existing secrets kept")
    else:
        data = generate()
        write(SECRETS_FILE, json.dumps(data, indent=2), 0o600, APP_UID)
        print("stand keys: new secrets generated")
    # производные файлы читают контейнеры postgres (uid 70) и minio — только внутри тома стенда
    write(STATE / "pg_password", str(data["pg_superuser_password"]), 0o644)
    write(STATE / "minio_user", str(data["s3_access_key"]), 0o644)
    write(STATE / "minio_password", str(data["s3_secret_key"]), 0o644)
    os.chmod(STATE, 0o755)  # noqa: S103 — каталог проходим для postgres/minio; secrets.json читает только пользователь приложения
    return 0


if __name__ == "__main__":
    sys.exit(main())
