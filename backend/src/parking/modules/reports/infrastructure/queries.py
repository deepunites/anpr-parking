"""SQL отчётов. Владелец парковки — сессия пула bulk с контекстом клиента (RLS); владелец платформы — maintenance.

Фильтр площадок — статический: NULL-параметр означает «без ограничения» (фрагменты — константы из литералов)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.reports.application.ports import (
    BarrierRow,
    CameraRow,
    HourRow,
    LocalHealth,
    LotRow,
    LotScope,
    ManualRow,
    PlatformTenant,
    RevenueRow,
    ShiftRow,
    StatusPoint,
    TenantInfo,
    Totals,
)

_LOT = "(CAST(:lot AS uuid) IS NULL OR lot_id = CAST(:lot AS uuid)) AND (CAST(:lots AS uuid[]) IS NULL OR lot_id = ANY(CAST(:lots AS uuid[])))"
_LOT_L = "(CAST(:lot AS uuid) IS NULL OR l.id = CAST(:lot AS uuid)) AND (CAST(:lots AS uuid[]) IS NULL OR l.id = ANY(CAST(:lots AS uuid[])))"
_PAID = "status IN ('PAID', 'REFUNDED') AND amount > 0 AND paid_at >= :a AND paid_at < :b"
_USER_NAME = "coalesce(u.display_name, u.email, u.phone, '—')"
MANUAL_ACTIONS = [
    "BARRIER_OPEN_MANUAL",
    "EXIT_OPENED_MANUAL",
    "MANUAL_ENTRY",
    "ENTRY_CONFIRMED_MANUAL",
    "PLATE_CORRECTED",
    "SESSION_CLOSED_MANUAL",
    "SESSION_ORPHANED_MANUAL",
]


def _p(scope: LotScope, **extra: Any) -> dict[str, Any]:
    return {"lot": scope.lot_id, "lots": list(scope.lots) if scope.lots is not None else None, **extra}


class SqlOwnerQueries:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def _rows(self, sql: str, params: dict[str, Any]) -> list[Any]:
        return list((await self._s.execute(text(sql), params)).all())

    async def tenant(self, tenant_id: UUID) -> TenantInfo:
        r = (
            await self._rows(
                """
                SELECT t.timezone, t.currency, t.plan_code, p.name AS plan_name, p.max_lots, p.max_places, p.max_cameras,
                       (SELECT count(*) FROM parking_lot l WHERE l.tenant_id = t.id AND l.active) AS lots,
                       (SELECT coalesce(sum(capacity), 0) FROM parking_lot l WHERE l.tenant_id = t.id AND l.active) AS places,
                       (SELECT count(*) FROM camera c WHERE c.tenant_id = t.id AND c.active) AS cameras
                FROM tenant t JOIN platform_plan p ON p.code = t.plan_code WHERE t.id = :t
                """,
                {"t": tenant_id},
            )
        )[0]
        return TenantInfo(
            timezone=r.timezone,
            currency=r.currency,
            plan_code=r.plan_code,
            plan_name=r.plan_name,
            max_lots=r.max_lots,
            max_places=r.max_places,
            max_cameras=r.max_cameras,
            lots=int(r.lots),
            places=int(r.places),
            cameras=int(r.cameras),
        )

    async def revenue(self, scope: LotScope, a: datetime, b: datetime, tz: str) -> list[RevenueRow]:
        rows = await self._rows(
            f"""
            SELECT (paid_at AT TIME ZONE :tz)::date AS day, method::text AS method, sum(amount) AS amount, count(*) AS n
            FROM payment WHERE {_PAID} AND {_LOT} GROUP BY 1, 2
            """,
            _p(scope, a=a, b=b, tz=tz),
        )
        return [RevenueRow(r.day, r.method, int(r.amount), int(r.n)) for r in rows]

    async def totals(self, scope: LotScope, a: datetime, b: datetime) -> Totals:
        r = (
            await self._rows(
                f"""
                SELECT
                  (SELECT coalesce(sum(amount), 0) FROM payment WHERE {_PAID} AND {_LOT}) AS gross,
                  (SELECT count(*) FROM payment WHERE {_PAID} AND {_LOT}) AS payments,
                  (SELECT coalesce(sum(r.amount), 0) FROM refund r JOIN payment p ON p.id = r.payment_id
                    WHERE r.status = 'DONE' AND r.completed_at >= :a AND r.completed_at < :b
                      AND (CAST(:lot AS uuid) IS NULL OR p.lot_id = CAST(:lot AS uuid))
                      AND (CAST(:lots AS uuid[]) IS NULL OR p.lot_id = ANY(CAST(:lots AS uuid[])))) AS refunds,
                  (SELECT count(*) FROM parking_session WHERE entry_time >= :a AND entry_time < :b AND {_LOT}) AS entries,
                  (SELECT count(*) FROM parking_session
                    WHERE status = 'CLOSED' AND exit_time >= :a AND exit_time < :b AND entry_time < :b AND {_LOT}) AS exits,
                  (SELECT avg(extract(epoch FROM exit_time - entry_time) / 60) FROM parking_session
                    WHERE status = 'CLOSED' AND exit_time >= :a AND exit_time < :b AND entry_time < :b AND {_LOT}) AS avg_minutes,
                  (SELECT count(*) FROM parking_session WHERE status = 'MANUAL_REVIEW' AND {_LOT}) AS review
                """,
                _p(scope, a=a, b=b),
            )
        )[0]
        return Totals(
            gross=int(r.gross),
            refunds=int(r.refunds),
            payments=int(r.payments),
            entries=int(r.entries),
            exits=int(r.exits),
            avg_minutes=round(float(r.avg_minutes), 1) if r.avg_minutes is not None else None,
            review=int(r.review),
        )

    async def lots(self, scope: LotScope, a: datetime, b: datetime) -> list[LotRow]:
        rows = await self._rows(
            f"""
            SELECT l.id, l.name, l.capacity, coalesce(rv.s, 0) AS revenue, coalesce(en.n, 0) AS entries
            FROM parking_lot l
            LEFT JOIN (SELECT lot_id, sum(amount) AS s FROM payment WHERE {_PAID} GROUP BY lot_id) rv ON rv.lot_id = l.id
            LEFT JOIN (SELECT lot_id, count(*) AS n FROM parking_session WHERE entry_time >= :a AND entry_time < :b
                       GROUP BY lot_id) en ON en.lot_id = l.id
            WHERE l.active AND {_LOT_L}
            ORDER BY revenue DESC, l.name
            """,
            _p(scope, a=a, b=b),
        )
        return [LotRow(r.id, r.name, int(r.capacity), int(r.revenue), int(r.entries)) for r in rows]

    async def hours(self, scope: LotScope, a: datetime, b: datetime, tz: str) -> list[HourRow]:
        rows = await self._rows(
            f"""
            SELECT h, sum(en) AS entries, sum(ex) AS exits FROM (
              SELECT extract(hour FROM entry_time AT TIME ZONE :tz)::int AS h, 1 AS en, 0 AS ex
              FROM parking_session WHERE entry_time >= :a AND entry_time < :b AND {_LOT}
              UNION ALL
              SELECT extract(hour FROM exit_time AT TIME ZONE :tz)::int, 0, 1
              FROM parking_session WHERE exit_time >= :a AND exit_time < :b AND entry_time < :b AND {_LOT}
            ) x GROUP BY h
            """,
            _p(scope, a=a, b=b, tz=tz),
        )
        return [HourRow(int(r.h), int(r.entries), int(r.exits)) for r in rows]

    async def manual(self, scope: LotScope, a: datetime, b: datetime) -> list[ManualRow]:
        rows = await self._rows(
            f"""
            SELECT a.user_id, {_USER_NAME} AS name, u.role::text AS role, a.action, count(*) AS n
            FROM audit_log a LEFT JOIN app_user u ON u.id = a.user_id
            WHERE a.created_at >= :a AND a.created_at < :b AND a.action = ANY(:actions)
              AND (CAST(:lot AS uuid) IS NULL OR a.lot_id = CAST(:lot AS uuid))
              AND (CAST(:lots AS uuid[]) IS NULL OR a.lot_id = ANY(CAST(:lots AS uuid[])))
            GROUP BY a.user_id, name, role, a.action
            """,
            _p(scope, a=a, b=b, actions=MANUAL_ACTIONS),
        )
        return [ManualRow(r.user_id, r.name, r.role, r.action, int(r.n)) for r in rows]

    async def shifts(self, scope: LotScope, a: datetime, b: datetime) -> list[ShiftRow]:
        rows = await self._rows(
            f"""
            SELECT s.id, l.name AS lot_name, {_USER_NAME} AS cashier, s.opened_at, s.closed_at,
                   coalesce(s.expected_cash, 0) AS expected, coalesce(s.counted_cash, 0) AS counted,
                   coalesce(s.discrepancy, 0) AS discrepancy
            FROM cashier_shift s JOIN parking_lot l ON l.id = s.lot_id LEFT JOIN app_user u ON u.id = s.cashier_id
            WHERE s.status = 'CLOSED' AND s.closed_at >= :a AND s.closed_at < :b AND coalesce(s.discrepancy, 0) <> 0
              AND {_LOT_L}
            ORDER BY s.closed_at DESC LIMIT 100
            """,
            _p(scope, a=a, b=b),
        )
        return [
            ShiftRow(
                r.id,
                r.lot_name,
                r.cashier,
                r.opened_at,
                r.closed_at,
                int(r.expected),
                int(r.counted),
                int(r.discrepancy),
            )
            for r in rows
        ]

    async def cameras(self, scope: LotScope) -> list[CameraRow]:
        rows = await self._rows(
            f"""
            SELECT c.id, c.name, l.name AS lot_name, c.status::text AS status
            FROM camera c JOIN parking_lot l ON l.id = c.lot_id
            WHERE c.active AND {_LOT_L} ORDER BY l.name, c.name
            """,
            _p(scope),
        )
        return [CameraRow(r.id, r.name, r.lot_name, r.status) for r in rows]

    async def status_history(
        self, scope: LotScope, a: datetime, b: datetime
    ) -> tuple[dict[UUID, str], list[StatusPoint]]:
        initial = await self._rows(
            f"""
            SELECT DISTINCT ON (camera_id) camera_id, status::text AS status FROM camera_status_log
            WHERE changed_at <= :a AND {_LOT} ORDER BY camera_id, changed_at DESC
            """,
            _p(scope, a=a),
        )
        points = await self._rows(
            f"""
            SELECT camera_id, changed_at, status::text AS status FROM camera_status_log
            WHERE changed_at > :a AND changed_at < :b AND {_LOT} ORDER BY changed_at LIMIT 50000
            """,
            _p(scope, a=a, b=b),
        )
        return {r.camera_id: r.status for r in initial}, [
            StatusPoint(r.camera_id, r.changed_at, r.status) for r in points
        ]

    async def barriers(self, scope: LotScope, a: datetime, b: datetime) -> list[BarrierRow]:
        rows = await self._rows(
            f"""
            SELECT br.id, br.name, l.name AS lot_name, count(c.id) AS commands,
                   count(c.id) FILTER (WHERE c.state = 'FAILED') AS failed, avg(c.latency_ms)::int AS latency
            FROM barrier br JOIN parking_lot l ON l.id = br.lot_id
            LEFT JOIN barrier_command c ON c.barrier_id = br.id AND c.created_at >= :a AND c.created_at < :b
                                        AND c.state IN ('OK', 'FAILED')
            WHERE br.active AND {_LOT_L}
            GROUP BY br.id, br.name, l.name ORDER BY l.name, br.name
            """,
            _p(scope, a=a, b=b),
        )
        return [BarrierRow(r.id, r.name, r.lot_name, int(r.commands), int(r.failed), r.latency) for r in rows]


class SqlPlatformQueries:
    """Роль maintenance: агрегаты по клиентам (A-14 — без данных водителей)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def tenants(self, now: datetime) -> list[PlatformTenant]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    text(
                        """
                        SELECT t.id, t.name, t.plan_code, p.price_minor, t.status, t.trial_ends_at, t.paid_until, t.created_at,
                          (SELECT count(*) FROM parking_lot l WHERE l.tenant_id = t.id AND l.active) AS lots,
                          (SELECT count(*) FROM camera c WHERE c.tenant_id = t.id AND c.active) AS cameras,
                          (SELECT count(*) FROM camera c WHERE c.tenant_id = t.id AND c.active
                             AND c.status IN ('OFFLINE', 'SILENT')) AS offline,
                          (SELECT count(*) FROM parking_session ps WHERE ps.tenant_id = t.id
                             AND ps.entry_time > :since) AS sessions_7d
                        FROM tenant t JOIN platform_plan p ON p.code = t.plan_code
                        ORDER BY t.created_at DESC
                        """
                    ),
                    {"since": now - timedelta(days=7)},
                )
            ).all()
        return [
            PlatformTenant(
                tenant_id=r.id,
                name=r.name,
                plan_code=r.plan_code,
                price=int(r.price_minor) if r.price_minor is not None else None,
                status=r.status,
                trial_ends_at=r.trial_ends_at,
                paid_until=r.paid_until,
                created_at=r.created_at,
                lots=int(r.lots),
                cameras=int(r.cameras),
                cameras_offline=int(r.offline),
                sessions_7d=int(r.sessions_7d),
            )
            for r in rows
        ]

    async def signups(self, since: datetime) -> list[tuple[date, int]]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT date_trunc('week', created_at)::date AS week, count(*) AS n FROM tenant "
                        "WHERE created_at >= :since GROUP BY 1 ORDER BY 1"
                    ),
                    {"since": since},
                )
            ).all()
        return [(r.week, int(r.n)) for r in rows]

    async def local_health(self, now: datetime) -> LocalHealth:
        async with self._factory() as s:
            r = (
                await s.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM outbox WHERE status = 'PENDING') AS pending,
                          (SELECT extract(epoch FROM :now - min(created_at))::int FROM outbox WHERE status = 'PENDING') AS oldest,
                          (SELECT count(*) FROM webhook_delivery WHERE status = 'DEAD' AND created_at > :day) AS dead,
                          (SELECT count(*) FROM camera WHERE active AND status IN ('OFFLINE', 'SILENT')) AS offline,
                          (SELECT count(*) FROM camera WHERE active) AS cameras
                        """
                    ),
                    {"now": now, "day": now - timedelta(days=1)},
                )
            ).one()
        return LocalHealth(
            outbox_pending=int(r.pending),
            outbox_oldest_seconds=int(r.oldest) if r.oldest is not None else None,
            webhooks_dead_24h=int(r.dead),
            cameras_offline=int(r.offline),
            cameras_total=int(r.cameras),
        )
