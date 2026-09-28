"""Здоровье камер (фаза 4 §2): раз в 30 с по Redis-heartbeat, не на каждом heartbeat. Флаш last_heartbeat_at /
last_event_at / last_used_at — батчем ролью app_bulk; смена статуса — редкое событие, пишется app_critical
вместе с devices.camera.status_changed.v1 в outbox (той же транзакцией)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from parking.modules.devices.application.ports import (
    CameraHealthRow,
    CameraHealthStore,
    CameraHealthWriter,
    HeartbeatStore,
)
from parking.modules.devices.domain.camera import CameraStatus, compute_status
from parking.platform import metrics
from parking.platform.db.engine import Pool
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.shared.clock import Clock

log = get_logger("devices.health")

HEARTBEAT_TTL_FACTOR = 10  # ключ heartbeat живёт 10 интервалов: дольше — камера и так OFFLINE по PostgreSQL


@dataclass(slots=True)
class HealthReport:
    checked: int = 0
    changed: list[tuple[UUID, CameraStatus, CameraStatus]] = field(default_factory=list)
    flushed_heartbeats: int = 0
    flushed_events: int = 0
    flushed_keys: int = 0


class CameraHealthJob:
    def __init__(
        self,
        *,
        store: CameraHealthStore,
        heartbeats: HeartbeatStore,
        uow_factory: Callable[[UUID | None, Pool], UnitOfWork],
        writer: CameraHealthWriter,
        clock: Clock,
    ) -> None:
        self._store = store
        self._heartbeats = heartbeats
        self._uow_factory = uow_factory
        self._writer = writer
        self._clock = clock

    async def run_once(self) -> HealthReport:
        report = HealthReport()
        rows = await self._store.active_cameras()
        ids = [r.camera_id for r in rows]
        redis_hb = await self._heartbeats.read_many(ids)
        redis_events = await self._heartbeats.last_events(ids)
        key_usage = await self._heartbeats.drain_key_usage()
        now = self._clock.now()
        by_tenant: dict[UUID, list[CameraHealthRow]] = defaultdict(list)
        for row in rows:
            by_tenant[row.tenant_id].append(row)
        counts: dict[CameraStatus, int] = defaultdict(int)
        by_lot: dict[tuple[UUID, CameraStatus], int] = defaultdict(int)
        for tenant_id, tenant_rows in by_tenant.items():
            heartbeats = [(r.camera_id, redis_hb[r.camera_id].at) for r in tenant_rows if r.camera_id in redis_hb]
            events = [(r.camera_id, redis_events[r.camera_id]) for r in tenant_rows if r.camera_id in redis_events]
            changes: list[tuple[CameraHealthRow, CameraStatus]] = []
            for r in tenant_rows:
                last_hb = _latest(r.last_heartbeat_at, redis_hb[r.camera_id].at if r.camera_id in redis_hb else None)
                last_event = _latest(r.last_event_at, redis_events.get(r.camera_id))
                gap = await self._heartbeats.expected_gap_seconds(r.camera_id, now.astimezone(UTC).hour)
                new_status = compute_status(
                    now=now,
                    interval_sec=r.heartbeat_interval_sec,
                    last_heartbeat_at=last_hb,
                    last_event_at=last_event,
                    expected_event_gap=timedelta(seconds=3 * gap) if gap is not None else None,
                )
                counts[new_status] += 1
                by_lot[(r.lot_id, new_status)] += 1
                report.checked += 1
                if new_status is not r.status:
                    changes.append((r, new_status))
            await self._flush_tenant(tenant_id, heartbeats, events, key_usage, tenant_rows, report)
            if changes:
                await self._apply_changes(tenant_id, changes, report)
        for status in CameraStatus:
            metrics.camera_status_total.labels(status=status.value).set(counts.get(status, 0))
        for lot_id in {r.lot_id for r in rows}:
            for status in CameraStatus:
                metrics.camera_status_by_lot.labels(lot=str(lot_id), status=status.value).set(
                    by_lot.get((lot_id, status), 0)
                )
        return report

    async def _flush_tenant(
        self,
        tenant_id: UUID,
        heartbeats: list[tuple[UUID, datetime]],
        events: list[tuple[UUID, datetime]],
        key_usage: dict[UUID, datetime],
        rows: list[CameraHealthRow],
        report: HealthReport,
    ) -> None:
        usage = [(kid, at) for kid, at in key_usage.items()]  # RLS отфильтрует чужие ключи в tenant-контексте
        if not (heartbeats or events or usage):
            return
        async with self._uow_factory(tenant_id, Pool.BULK) as uow:
            await self._writer.flush(uow, heartbeats=heartbeats, events=events, key_usage=usage)
        report.flushed_heartbeats += len(heartbeats)
        report.flushed_events += len(events)
        report.flushed_keys += len(usage)

    async def _apply_changes(
        self, tenant_id: UUID, changes: list[tuple[CameraHealthRow, CameraStatus]], report: HealthReport
    ) -> None:
        async with self._uow_factory(tenant_id, Pool.CRITICAL) as uow:
            for row, new_status in changes:
                await self._writer.update_status(uow, row.camera_id, new_status)
                uow.publish(
                    IntegrationEvent(
                        type="devices.camera.status_changed.v1",
                        tenant_id=tenant_id,
                        aggregate_type="camera",
                        aggregate_id=row.camera_id,
                        payload={
                            "camera_id": str(row.camera_id),
                            "lot_id": str(row.lot_id),
                            "old_status": row.status.value,
                            "new_status": new_status.value,
                        },
                    )
                )
                level = log.error if new_status is CameraStatus.OFFLINE else log.warning
                level(
                    "camera_status_changed",
                    camera_id=str(row.camera_id),
                    lot_id=str(row.lot_id),
                    old=row.status.value,
                    new=new_status.value,
                )
                report.changed.append((row.camera_id, row.status, new_status))


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
