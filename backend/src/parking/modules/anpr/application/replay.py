"""Воспроизведение оффлайн-буфера (фаза 4 §4): consumer group `offline`, батч ≤ 1000, сортировка по event_time,
XAUTOCLAIM зависших, экспоненциальный ретрай, DLQ после N попыток.

Порядок в стриме не гарантирует порядок событий (EXIT может прийти раньше ENTRY): батч сортируется по event_time,
а parking допускает сессии задним числом — EXIT без сессии создаёт её как `entry_inferred` (A-10), поздний ENTRY
с event_time раньше exit_time присоединяется к ней. Redis — не источник истины: после replay всё в PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from parking.modules.anpr.application.ingest import IngestService
from parking.modules.anpr.application.ports import BufferedEntry, OfflineBuffer
from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.correlation import correlation_scope
from parking.shared.errors import ServiceUnavailableError

log = get_logger("anpr.replay")


@dataclass(slots=True)
class ReplayReport:
    processed: int = 0
    duplicates: int = 0
    failed: int = 0
    dead_lettered: int = 0
    deferred: int = 0  # БД всё ещё недоступна — батч отложен без ACK
    entry_ids: list[str] = field(default_factory=list)


class OfflineReplayer:
    def __init__(
        self,
        *,
        buffer: OfflineBuffer,
        ingest: IngestService,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        audit: Callable[[UnitOfWork], AuditWriter],
        is_connectivity_error: Callable[[BaseException], bool],
        consumer_name: str = "replayer-1",
        batch: int = 1000,
        max_attempts: int = 3,
        claim_idle_ms: int = 60_000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._buffer = buffer
        self._ingest = ingest
        self._uow_factory = uow_factory
        self._audit = audit
        self._is_connectivity_error = is_connectivity_error
        self._name = consumer_name
        self._batch = batch
        self._max_attempts = max_attempts
        self._claim_idle_ms = claim_idle_ms
        self._sleep = sleep
        self._backoff = 0
        self._group_ready = False

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            report = await self.poll_once()
            if report.deferred:
                self._backoff = min(self._backoff + 1, 6)
                await self._sleep(2**self._backoff)  # 2, 4, … 64 с: БД лежит — не долбить
            elif report.processed == 0:
                self._backoff = 0
                await self._sleep(0.5)
            else:
                self._backoff = 0

    async def poll_once(self) -> ReplayReport:
        """Один проход: зависшие (XAUTOCLAIM) → собственные незавершённые (id 0) → новые (>), всё одним батчем."""
        report = ReplayReport()
        if not self._group_ready:
            await self._buffer.ensure_group()
            self._group_ready = True
        entries = await self._buffer.claim_stale(self._name, min_idle_ms=self._claim_idle_ms, count=self._batch)
        entries += await self._buffer.read(self._name, count=self._batch, own_pending=True)
        if len(entries) < self._batch:
            entries += await self._buffer.read(self._name, count=self._batch - len(entries), own_pending=False)
        seen: set[str] = set()
        unique = [e for e in entries if not (e.entry_id in seen or seen.add(e.entry_id))]  # type: ignore[func-returns-value]
        unique.sort(key=lambda e: e.fields.get("event_time", ""))
        acked: list[str] = []
        for entry in unique:
            outcome = await self._replay_one(entry, report)
            if outcome == "deferred":
                report.deferred += 1
                break
            acked.append(entry.entry_id)
        await self._buffer.ack(acked)
        report.entry_ids = acked
        metrics.redis_stream_lag.labels(stream="anpr:offline").set(await self._buffer.pending())
        metrics.offline_queue_length.set(await self._buffer.length())
        return report

    async def _replay_one(self, entry: BufferedEntry, report: ReplayReport) -> str:
        fields = entry.fields
        attempt = int(fields.get("retry_count", "0")) + 1
        with correlation_scope(fields.get("correlation_id") or None):
            try:
                response = await self._ingest.process_buffered(fields)
            except ServiceUnavailableError:
                return "deferred"
            except Exception as exc:
                if self._is_connectivity_error(exc):
                    return "deferred"
                report.failed += 1
                metrics.offline_replayed_total.labels(result="error").inc()
                log.warning(
                    "offline_replay_failed",
                    entry_id=entry.entry_id,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                )
                if attempt >= self._max_attempts:
                    await self._buffer.to_dlq(entry, f"{type(exc).__name__}: {exc}")
                    report.dead_lettered += 1
                    log.error("offline_replay_dead_lettered", entry_id=entry.entry_id, attempts=attempt)
                else:
                    await self._buffer.enqueue({**fields, "retry_count": str(attempt)})
                return "requeued"
            report.processed += 1
            if response.duplicate:
                report.duplicates += 1
            metrics.offline_replayed_total.labels(result="duplicate" if response.duplicate else "ok").inc()
            if fields.get("barrier_opened_offline") == "1" and not response.duplicate:
                await self._audit_offline_open(fields, response.session_id)
            return "ok"

    async def _audit_offline_open(self, fields: dict[str, str], session_id: UUID | None) -> None:
        """Шлагбаум открывался в оффлайне без записи barrier_command (БД не было): след — в аудите при replay."""
        tenant_id = UUID(fields["tenant_id"])
        async with self._uow_factory(tenant_id) as uow:
            await self._audit(uow).record(
                AuditEntry(
                    tenant_id=tenant_id,
                    action="BARRIER_OPEN_OFFLINE",
                    entity_type="camera",
                    entity_id=UUID(fields["camera_id"]),
                    lot_id=UUID(fields["lot_id"]),
                    session_id=session_id,
                    actor_type=ActorType.SYSTEM,
                    reason="offline mode: barrier opened from cached decision (A-10)",
                    payload={
                        "event_id": fields["event_id"],
                        "event_type": fields["event_type"],
                        "received_at": fields["received_at"],
                    },
                )
            )


def sort_key(entry: BufferedEntry) -> datetime:
    return datetime.fromisoformat(entry.fields["event_time"])
