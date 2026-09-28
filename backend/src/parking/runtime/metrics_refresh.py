"""Gauge-метрики, вычисляемые на scrape `/metrics` (фаза 7 §2): занятость по площадкам, outbox/стримы/DLQ со стороны
API (алерт лага outbox срабатывает, даже если publisher мёртв и его собственные метрики исчезли), открытые
расхождения сверки по видам, доступность зависимостей. Не чаще раза в 5 с, ошибки не роняют scrape."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.parking.application.ports import OccupancyProjection
from parking.platform import metrics
from parking.platform.breaker import BreakerState, CircuitBreaker, CircuitOpenError
from parking.platform.logging import get_logger
from parking.workers.envelope import stream_name

log = get_logger("metrics")
DLQ_STREAMS = ("events:dlq", "anpr:dlq")
_OUTBOX_SQL = text(
    "SELECT count(*) AS pending, coalesce(extract(epoch FROM (now() - min(created_at))), 0) AS age "
    "FROM outbox WHERE status = 'PENDING'"
)
_OPEN_DISCREPANCIES_SQL = text(
    "SELECT kind::text AS kind, count(*) AS n FROM reconciliation_discrepancy WHERE status = 'OPEN' GROUP BY kind"
)


class MetricsRefresher:
    def __init__(
        self,
        maintenance_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        occupancy: OccupancyProjection,
        *,
        db_breaker: CircuitBreaker | None = None,
        shards: int = 8,
        min_interval: float = 5.0,
        probe_timeout: float = 3.0,
    ) -> None:
        self._factory = maintenance_factory
        self._redis = redis
        self._occupancy = occupancy
        self._db_breaker = db_breaker
        self._shards = shards
        self._min_interval = min_interval
        self._probe_timeout = probe_timeout
        self._last = 0.0
        self._lots: list[tuple[UUID, int]] = []
        self._lots_at = 0.0

    async def refresh(self) -> None:
        now = time.monotonic()
        if now - self._last < self._min_interval:
            return
        self._last = now
        started = time.perf_counter()
        # scrape не должен зависать вместе с зависимостью: таймаут → dependency_up=0
        for dependency, probe in (("postgres", self._postgres), ("redis", self._redis_side)):
            try:
                await asyncio.wait_for(probe(), timeout=self._probe_timeout)
            except TimeoutError:
                metrics.dependency_up.labels(dependency=dependency).set(0)
                log.warning("metrics_refresh_timeout", dependency=dependency, timeout=self._probe_timeout)
        metrics.metrics_refresh_seconds.observe(time.perf_counter() - started)

    async def _postgres(self) -> None:
        try:
            async with self._factory() as session, session.begin():
                if time.monotonic() - self._lots_at > 60:
                    rows = await session.execute(text("SELECT id, capacity FROM parking_lot WHERE active"))
                    self._lots = [(r.id, int(r.capacity)) for r in rows]
                    self._lots_at = time.monotonic()
                outbox = (await session.execute(_OUTBOX_SQL)).first()
                metrics.outbox_pending_total.set(int(outbox.pending) if outbox else 0)
                metrics.outbox_oldest_pending_age_seconds.set(float(outbox.age) if outbox else 0.0)
                open_by_kind = await session.execute(_OPEN_DISCREPANCIES_SQL)
                found = {r.kind: int(r.n) for r in open_by_kind}
                for kind in (
                    "MISSING_LOCAL",
                    "MISSING_REMOTE",
                    "AMOUNT_MISMATCH",
                    "STATUS_MISMATCH",
                    "DUPLICATE_REMOTE",
                ):
                    metrics.reconciliation_discrepancies_open.labels(kind=kind).set(found.get(kind, 0))
            metrics.dependency_up.labels(dependency="postgres").set(1)
            self._breaker_probe_succeeded()
        except Exception as exc:
            metrics.dependency_up.labels(dependency="postgres").set(0)
            log.warning("metrics_refresh_postgres_failed", error_type=type(exc).__name__)
        self._publish_breaker_state()

    def _breaker_probe_succeeded(self) -> None:
        """Фоновая проба вместо трафика: без неё DB-breaker после аварии закрылся бы только следующим событием камеры,
        а offline_mode_active оставался бы 1 (алерт PostgresPrimaryDown не гас бы при тишине)."""
        breaker = self._db_breaker
        if breaker is None or breaker.state is BreakerState.CLOSED or breaker.is_open:
            return  # OPEN до истечения ttl — уважаем задержку breaker'а
        try:
            breaker.acquire()  # OPEN(ttl истёк) → HALF_OPEN → проба
        except CircuitOpenError:
            return
        breaker.record_success()

    def _publish_breaker_state(self) -> None:
        if self._db_breaker is not None:
            metrics.offline_mode_active.set(1 if self._db_breaker.state is BreakerState.OPEN else 0)

    async def _redis_side(self) -> None:
        try:
            await self._redis.ping()
            for lot_id, capacity in self._lots:
                occupied = await self._occupancy.get(lot_id)
                if occupied is None:
                    continue
                metrics.session_active.labels(lot=str(lot_id)).set(occupied)
                metrics.occupancy_ratio.labels(lot=str(lot_id)).set(occupied / capacity if capacity else 0.0)
            groups = [(stream_name(shard), "core") for shard in range(self._shards)] + [("anpr:offline", "offline")]
            for stream, group in groups:
                metrics.stream_pending_total.labels(stream=stream).set(await self._group_lag(stream, group))
            for dlq in DLQ_STREAMS:
                metrics.dlq_length.labels(stream=dlq).set(int(await self._redis.xlen(dlq)))
            metrics.dependency_up.labels(dependency="redis").set(1)
        except Exception as exc:
            metrics.dependency_up.labels(dependency="redis").set(0)
            log.warning("metrics_refresh_redis_failed", error_type=type(exc).__name__)

    async def _group_lag(self, stream: str, group: str) -> int:
        """Отставание consumer group: не доставленные записи (XINFO GROUPS lag, Redis ≥ 7) + доставленные без ACK.
        Один XPENDING видел бы только второе слагаемое: остановившийся consumer давал бы 0 при растущем стриме
        (нагрузочный прогон фазы 8)."""
        try:
            for info in await self._redis.xinfo_groups(stream):
                if info.get("name") == group.encode():
                    return int(info.get("lag") or 0) + int(info.get("pending") or 0)
        except ResponseError:
            return 0
        return 0


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
