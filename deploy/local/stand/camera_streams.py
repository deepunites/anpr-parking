"""Видеопотоки камер Hikvision для круглосуточной записи на диск сервера площадки (deploy/truenas/video.sh).

Камера DS-TCG406-E не пишет видео на сетевой диск (в прошивке нет NAS) — запись ведёт сервер: забирает RTSP-поток
камеры и режет его на файлы. Логин и пароль камеры уже есть у её стрелы (драйвер HIKVISION_ISAPI, зашифрованы
ключом клиента) — второй раз их не вводят. Печатает строки «<папка> <rtsp-адрес>»: папка cam-entry / cam-exit по
назначению камеры (вторая камера того же назначения — cam-entry-2 …). В выводе пароли — video.sh пишет его в файл
с правами 600, на экран не выводит.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

import asyncpg
from standlib import load_secrets, superuser_dsn

from parking.modules.devices.domain.endpoint import parse_endpoint
from parking.platform.config import load_settings
from parking.platform.db.engine import build_database
from parking.platform.secret_box import TenantSecretBox
from parking.runtime.container import build_container

RTSP_PORT = 554
MAIN_STREAM = "101"  # 101 — основной поток (полное разрешение), 102 — дополнительный
FOLDER = {"IN": "cam-entry", "OUT": "cam-exit"}

_STREAMS = """
    SELECT b.id, b.tenant_id, b.endpoint, b.credentials_encrypted, c.code, c.direction::text AS direction
    FROM barrier b JOIN camera c ON c.id = b.camera_id
    WHERE b.active AND c.active AND b.driver = 'HIKVISION_ISAPI' AND b.credentials_encrypted IS NOT NULL
    ORDER BY c.direction, c.code, b.id
"""


@dataclass(frozen=True, slots=True)
class Stream:
    barrier_id: UUID
    camera_code: str
    folder: str
    url: str


def rtsp_url(endpoint: str, username: str, password: str, channel: str = MAIN_STREAM) -> str:
    """rtsp://логин:пароль@адрес-камеры:554/Streaming/Channels/101 — адрес из настроек стрелы (http://IP камеры)."""
    host = parse_endpoint(endpoint, driver="HIKVISION_ISAPI").host
    host = f"[{host}]" if ":" in host else host
    login = f"{quote(username, safe='')}:{quote(password, safe='')}"
    return f"rtsp://{login}@{host}:{RTSP_PORT}/Streaming/Channels/{channel}"


async def streams(container: Any, conn: asyncpg.Connection, channel: str = MAIN_STREAM) -> list[Stream]:
    """Камеры с действующей стрелой Hikvision и сохранёнными логином/паролем; conn — суперпользователь БД."""
    await container.refresh_tenant_keys()  # ключи клиентов, зарегистрированных после старта процесса
    box = TenantSecretBox(container.layered_keys)
    found: list[Stream] = []
    taken: dict[str, int] = {}
    for row in await conn.fetch(_STREAMS):
        creds = json.loads(box.open(row["tenant_id"], f"barrier:{row['id']}", bytes(row["credentials_encrypted"])))
        base = FOLDER.get(row["direction"], "cam")
        taken[base] = taken.get(base, 0) + 1
        folder = base if taken[base] == 1 else f"{base}-{taken[base]}"
        url = rtsp_url(row["endpoint"], creds["username"], creds["password"], channel)
        found.append(Stream(barrier_id=row["id"], camera_code=row["code"], folder=folder, url=url))
    return found


async def main(channel: str) -> None:
    settings = load_settings()  # окружение PARKING_* уже подставил entry.py
    database = build_database(settings)
    container = build_container(settings, database)
    conn = await asyncpg.connect(superuser_dsn(load_secrets()))
    try:
        found = await streams(container, conn, channel)
    finally:
        await conn.close()
        await container.aclose()
        await database.dispose()
    for stream in found:
        print(f"{stream.folder} {stream.url}")
    print(f"камер для записи: {len(found)}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else MAIN_STREAM))
