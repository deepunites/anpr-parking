"""Журнал событий камер (7 дней): записи копятся в памяти процесса и раз в секунду уходят пачкой (одна транзакция
на клиента, executemany) — горячий путь камеры не получает лишних запросов, нагрузка на БД не растёт с потоком.
Цена — до секунды задержки в журнале и потеря буфера при аварийном падении процесса (журнал вспомогательный)."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.integrations.application.ports import JournalRepository
from parking.modules.integrations.domain.journal import JournalEntry
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.clock import Clock

log = get_logger("integrations.journal")
RETENTION = timedelta(days=7)
FLUSH_EVERY = 1.0
FLUSH_AT = 200
BUFFER_MAX = 10_000  # больше — новые записи отбрасываются (журнал не должен съесть память при сбое БД)


class JournalService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repo: Callable[[UnitOfWork], JournalRepository],
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory  # пул bulk: журнал не конкурирует с приёмом событий за соединения
        self._repo = repo
        self._clock = clock
        self._buffer: list[JournalEntry] = []
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def enqueue(self, entry: JournalEntry) -> None:
        if len(self._buffer) >= BUFFER_MAX:
            self.dropped += 1
            metrics.event_journal_dropped_total.inc()
            return
        self._buffer.append(entry)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while self._buffer:
            if len(self._buffer) < FLUSH_AT:
                await asyncio.sleep(FLUSH_EVERY)
            await self.flush()

    async def flush(self) -> int:
        batch, self._buffer = self._buffer, []
        by_tenant: dict[UUID, list[JournalEntry]] = defaultdict(list)
        for entry in batch:
            by_tenant[entry.tenant_id].append(entry)
        for tenant_id, entries in by_tenant.items():
            try:
                async with self._uow_factory(tenant_id) as uow:
                    await self._repo(uow).add_many(entries)
                metrics.event_journal_written_total.inc(len(entries))
            except Exception as exc:  # журнал — вспомогательный: его сбой не должен ронять приём
                metrics.event_journal_dropped_total.inc(len(entries))
                log.warning("journal_write_failed", error_type=type(exc).__name__, lost=len(entries))
        return len(batch)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush()

    async def search(
        self,
        actor: CurrentUser,
        *,
        lot_id: UUID | None,
        outcome: str | None,
        correlation_id: UUID | None,
        camera_id: UUID | None,
        limit: int,
    ) -> list[JournalEntry]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repo(uow).search(
                lot_ids=actor.allowed_lots(),
                lot_id=lot_id,
                outcome=outcome,
                correlation_id=correlation_id,
                camera_id=camera_id,
                since=self._clock.now() - RETENTION,
                limit=min(max(limit, 1), 500),
            )
