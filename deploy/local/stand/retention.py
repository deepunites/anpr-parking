"""Обслуживание БД стенда: раз в час retention.sql — партиции вперёд (outbox/inbox — по дням) и удаление старых.

В production это делает планировщик кластера; на стенде без этого вставки в outbox упали бы через пару суток.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from standlib import load_secrets, superuser_dsn

SQL = Path(os.environ.get("STAND_SEED_DIR", "/seed")) / "retention.sql"
PERIOD = float(os.environ.get("STAND_RETENTION_SECONDS", "3600"))


async def main() -> None:
    while True:
        try:
            conn = await asyncpg.connect(superuser_dsn(load_secrets()), timeout=10)
            try:
                await conn.execute(SQL.read_text(encoding="utf-8"))  # несколько операторов — простой протокол
            finally:
                await conn.close()
            print(f"[maintenance] {datetime.now(UTC):%Y-%m-%d %H:%M} partitions ok", flush=True)
        except Exception as exc:  # БД перезапускается — повторим в следующий раз
            print(f"[maintenance] retention failed: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(PERIOD)


if __name__ == "__main__":
    asyncio.run(main())
