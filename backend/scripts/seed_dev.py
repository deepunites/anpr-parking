"""Довод сид-данных до рабочего состояния для dev: argon2-пароли, per-tenant ключи в .env.dev, реальные HMAC номеров.

Использование (после `alembic upgrade head` и `seed_dev.sql`):
    uv run python scripts/seed_dev.py
Пароли dev-пользователей печатаются один раз; .env.dev не коммитится.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from uuid import UUID

import asyncpg

from parking.modules.identity.infrastructure.argon2_hasher import Argon2Hasher
from parking.platform.crypto import EnvKeyProvider, PlateCrypto, hmac_camera_key
from parking.shared.plate import PlateNumber

BACKEND = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND / ".env.dev"
TENANTS = {"alpha": UUID("00000000-0000-4000-8000-0000000000a1"), "beta": UUID("00000000-0000-4000-8000-0000000000b1")}
# номера из seed_dev.sql (правила доступа и абонемент) — пересчитываем их хеши настоящим HMAC tenant
SEED_PLATES = {"alpha": ["01B666BB", "01V001VV", "01W333WW", "01A777AA"]}
CAMERA_KEYS = {
    "devkey_entry_01a": "00000000-0000-4000-8000-0000000d0101",
    "devkey_exit_01aa": "00000000-0000-4000-8000-0000000d0102",
    "devkey_entry_01b": "00000000-0000-4000-8000-0000000d0105",
}


def dsn() -> str:
    return os.environ.get("PARKING_SEED_DSN", f"postgresql://postgres@/parking_dev?host={BACKEND / '.pgdata'}")


def load_or_create_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    if "PARKING_TENANT_KEYS_JSON" not in env:
        env["PARKING_TENANT_KEYS_JSON"] = json.dumps(
            {str(t): EnvKeyProvider.generate_entry() for t in TENANTS.values()}
        )
    if "PARKING_CAMERA_KEY_PEPPER" not in env:
        env["PARKING_CAMERA_KEY_PEPPER"] = secrets.token_hex(32)
    env.setdefault("PARKING_ENV", "dev")
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    return env


async def main() -> None:
    env = load_or_create_env()
    crypto = PlateCrypto(EnvKeyProvider(env["PARKING_TENANT_KEYS_JSON"]))
    pepper = bytes.fromhex(env["PARKING_CAMERA_KEY_PEPPER"])
    hasher = Argon2Hasher()
    conn = await asyncpg.connect(dsn())
    try:
        users = await conn.fetch("SELECT id, email FROM app_user WHERE password_hash = 'dev-placeholder'")
        for row in users:
            password = "dev-" + secrets.token_urlsafe(9)
            await conn.execute("UPDATE app_user SET password_hash = $1 WHERE id = $2", hasher.hash(password), row["id"])
            print(f"{row['email']}: {password}")
        for code, tenant_id in TENANTS.items():
            for raw in SEED_PLATES.get(code, []):
                plate = PlateNumber.parse(raw)
                old = hashlib.sha256(raw.encode()).digest()
                new_hash = crypto.hash(tenant_id, plate)
                enc, version = crypto.encrypt(tenant_id, plate)
                await conn.execute(
                    "UPDATE access_rule SET plate_hash = $1, plate_encrypted = $2, enc_key_version = $3 WHERE tenant_id = $4 AND plate_hash = $5",
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
        for key, key_id in CAMERA_KEYS.items():
            await conn.execute(
                "UPDATE camera_api_key SET key_hmac = $1 WHERE id = $2", hmac_camera_key(pepper, key), UUID(key_id)
            )
        print(f"env written to {ENV_FILE}; camera dev keys: {', '.join(CAMERA_KEYS)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
