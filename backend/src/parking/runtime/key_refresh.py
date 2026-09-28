"""v2 этап 3: подхват ключей шифрования новых клиентов во всех процессах (API, воркеры) без перезапуска.

Периодически (settings.key_refresh_seconds) и вне очереди — при промахе в LayeredKeyProvider (не чаще раза в 2 с)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from uuid import UUID

from parking.platform.logging import get_logger
from parking.runtime.container import Container

log = get_logger("runtime.keys")

_MIN_GAP_SECONDS = 2.0


async def run_key_refresh(container: Container, stop: asyncio.Event) -> None:
    if container.key_wrapper is None:
        return
    wake = asyncio.Event()
    last = 0.0

    def on_miss(_tenant_id: UUID) -> None:
        if time.monotonic() - last > _MIN_GAP_SECONDS:
            wake.set()

    container.layered_keys.on_miss = on_miss
    while not stop.is_set():
        try:
            loaded = await container.refresh_tenant_keys()
            last = time.monotonic()
            log.debug("tenant_keys_refreshed", loaded=loaded)
        except Exception as exc:  # БД недоступна — ключи уже в памяти работают, попробуем позже
            log.warning("tenant_keys_refresh_failed", error_type=type(exc).__name__)
        wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_first(stop, wake), container.settings.key_refresh_seconds)


async def _first(*events: asyncio.Event) -> None:
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
