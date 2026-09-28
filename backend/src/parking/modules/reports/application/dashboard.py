"""Сборка дашбордов: владелец парковки (DASHBOARD_VIEW) и владелец платформы (PLATFORM_VIEW). Только чтение;
отчётные запросы идут в пул bulk (не мешают проезду машин), кросс-tenant сводка — роль maintenance."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.reports.application.ports import (
    BarrierRow,
    HealthSource,
    LocalHealth,
    LotRow,
    LotScope,
    OwnerQueries,
    PlatformQueries,
    PromHealth,
    ShiftRow,
)
from parking.modules.reports.domain.dashboard import (
    DEVICE_AVAILABILITY_MIN,
    Period,
    Signal,
    attention_reasons,
    availability,
    cash_signal,
    device_signal,
    limit_signals,
    manual_open_signal,
    mrr,
)
from parking.platform.db.uow import UnitOfWork
from parking.shared.clock import Clock
from parking.shared.errors import AuthorizationError

MANUAL_KINDS = {
    "BARRIER_OPEN_MANUAL": "opens",
    "EXIT_OPENED_MANUAL": "opens",
    "MANUAL_ENTRY": "entries",
    "ENTRY_CONFIRMED_MANUAL": "entries",
    "PLATE_CORRECTED": "corrections",
    "SESSION_CLOSED_MANUAL": "closes",
    "SESSION_ORPHANED_MANUAL": "closes",
}
MONEY_METHODS = ("CASH", "CARD", "ONLINE")


@dataclass(frozen=True, slots=True)
class Kpi:
    value: float
    previous: float

    @property
    def change(self) -> float | None:
        return None if not self.previous else round((self.value - self.previous) / self.previous, 4)


@dataclass(frozen=True, slots=True)
class DayRevenue:
    day: date
    total: int
    by_method: dict[str, int]


@dataclass(frozen=True, slots=True)
class OperatorManual:
    user_id: UUID | None
    name: str
    role: str | None
    opens: int = 0
    entries: int = 0
    corrections: int = 0
    closes: int = 0

    @property
    def total(self) -> int:
        return self.opens + self.entries + self.corrections + self.closes


@dataclass(frozen=True, slots=True)
class CameraAvailability:
    camera_id: UUID
    name: str
    lot_name: str
    status: str
    availability: float | None


@dataclass(frozen=True, slots=True)
class LimitUsage:
    kind: str
    used: int
    limit: int | None


@dataclass(frozen=True, slots=True)
class OwnerDashboard:
    period_start: date
    period_end: date
    timezone: str
    currency: str
    lot_id: UUID | None
    revenue: Kpi
    sessions: Kpi
    avg_check: Kpi
    payments: int
    refunds: int
    exits: int
    avg_minutes: float | None
    review: int
    revenue_by_day: list[DayRevenue]
    revenue_by_method: dict[str, int]
    lots: list[LotRow]
    load_by_hour: list[dict[str, float]]
    manual: list[OperatorManual]
    shifts: list[ShiftRow]
    cameras: list[CameraAvailability]
    barriers: list[BarrierRow]
    plan_code: str
    plan_name: str
    limits: list[LimitUsage]
    signals: list[Signal] = field(default_factory=list)


class OwnerDashboardService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        queries: Callable[[UnitOfWork], OwnerQueries],
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._queries = queries
        self._clock = clock

    async def build(
        self, actor: CurrentUser, *, start: date | None, end: date | None, lot_id: UUID | None, days: int = 7
    ) -> OwnerDashboard:
        allowed = actor.allowed_lots()
        if lot_id is not None and allowed is not None and lot_id not in allowed:
            raise AuthorizationError("lot is outside your scope")
        scope = LotScope(lot_id=lot_id, lots=allowed)
        async with self._uow_factory(actor.tenant_id) as uow:
            q = self._queries(uow)
            info = await q.tenant(actor.tenant_id)
            now = self._clock.now()
            period = Period.parse(start, end, info.timezone, now.astimezone(ZoneInfo(info.timezone)).date(), days=days)
            a, b = period.bounds()
            pa, pb = period.previous().bounds()
            revenue_rows = await q.revenue(scope, a, b, info.timezone)
            totals, prev = await q.totals(scope, a, b), await q.totals(scope, pa, pb)
            lots = await q.lots(scope, a, b)
            hours = await q.hours(scope, a, b, info.timezone)
            manual_rows = await q.manual(scope, a, b)
            shifts = await q.shifts(scope, a, b)
            cameras = await q.cameras(scope)
            initial, points = await q.status_history(scope, a, b)
            barriers = await q.barriers(scope, a, b)

        by_day: dict[date, dict[str, int]] = {d: {} for d in period.dates()}
        by_method: dict[str, int] = defaultdict(int)
        for r in revenue_rows:
            by_day.setdefault(r.day, {})[r.method] = by_day.get(r.day, {}).get(r.method, 0) + r.amount
            by_method[r.method] += r.amount
        net, prev_net = totals.gross - totals.refunds, prev.gross - prev.refunds
        avg_check = Kpi(
            round(totals.gross / totals.payments) if totals.payments else 0,
            round(prev.gross / prev.payments) if prev.payments else 0,
        )
        operators = _operators(manual_rows)
        top = operators[0] if operators and operators[0].opens else None
        history: dict[UUID, list[tuple[datetime, str]]] = defaultdict(list)
        for p in points:
            history[p.camera_id].append((p.changed_at, p.status))
        end_at = min(b, now)
        camera_rows = [
            CameraAvailability(
                c.camera_id,
                c.name,
                c.lot_name,
                c.status,
                availability(initial.get(c.camera_id), history.get(c.camera_id, []), a, end_at) if end_at > a else None,
            )
            for c in cameras
        ]
        limits = [
            LimitUsage("lots", info.lots, info.max_lots),
            LimitUsage("places", info.places, info.max_places),
            LimitUsage("cameras", info.cameras, info.max_cameras),
        ]
        signals = [
            s
            for s in (
                cash_signal([s.discrepancy for s in shifts]),
                manual_open_signal(
                    sum(o.opens for o in operators), totals.exits, top.name if top else None, top.opens if top else 0
                ),
                device_signal(
                    [
                        c.name
                        for c in camera_rows
                        if c.availability is not None and c.availability < DEVICE_AVAILABILITY_MIN
                    ]
                    + [b.name for b in barriers if b.commands and b.failed / b.commands > 1 - DEVICE_AVAILABILITY_MIN]
                ),
            )
            if s is not None
        ] + limit_signals([(u.kind, u.used, u.limit) for u in limits])
        days = period.days
        return OwnerDashboard(
            period_start=period.start,
            period_end=period.end,
            timezone=info.timezone,
            currency=info.currency,
            lot_id=lot_id,
            revenue=Kpi(net, prev_net),
            sessions=Kpi(totals.entries, prev.entries),
            avg_check=avg_check,
            payments=totals.payments,
            refunds=totals.refunds,
            exits=totals.exits,
            avg_minutes=totals.avg_minutes,
            review=totals.review,
            revenue_by_day=[DayRevenue(d, sum(m.values()), m) for d, m in sorted(by_day.items())],
            revenue_by_method=dict(by_method),
            lots=lots,
            load_by_hour=_hours(hours, days),
            manual=operators,
            shifts=shifts,
            cameras=camera_rows,
            barriers=barriers,
            plan_code=info.plan_code,
            plan_name=info.plan_name,
            limits=limits,
            signals=signals,
        )


def _unavailable(cameras: list[CameraAvailability], barriers: list[BarrierRow]) -> list[str]:
    down = [c.name for c in cameras if c.availability is not None and c.availability < DEVICE_AVAILABILITY_MIN]
    return down + [b.name for b in barriers if b.commands and b.failed / b.commands > 1 - DEVICE_AVAILABILITY_MIN]


def _operators(rows: list[Any]) -> list[OperatorManual]:
    acc: dict[UUID | None, dict[str, Any]] = {}
    for r in rows:
        kind = MANUAL_KINDS.get(r.action)
        if kind is None:
            continue
        item = acc.setdefault(r.user_id, {"user_id": r.user_id, "name": r.name, "role": r.role})
        item[kind] = item.get(kind, 0) + r.count
    return sorted((OperatorManual(**v) for v in acc.values()), key=lambda o: (-o.opens, -o.total, o.name))


def _hours(rows: list[Any], days: int) -> list[dict[str, float]]:
    table = {r.hour: r for r in rows}
    return [
        {
            "hour": h,
            "entries": round(table[h].entries / days, 2) if h in table else 0.0,
            "exits": round(table[h].exits / days, 2) if h in table else 0.0,
        }
        for h in range(24)
    ]


# ---------------------------------------------------------------- владелец платформы
@dataclass(frozen=True, slots=True)
class AttentionItem:
    tenant_id: UUID
    name: str
    status: str
    plan_code: str
    reasons: list[str]
    trial_ends_at: datetime | None
    paid_until: datetime | None


@dataclass(frozen=True, slots=True)
class PlatformDashboard:
    generated_at: datetime
    clients: dict[str, int]
    mrr: int
    contract_clients: int
    trials: int
    trials_ending: int
    signups_by_week: list[tuple[date, int]]
    by_plan: dict[str, int]
    attention: list[AttentionItem]
    health_source: str
    local: LocalHealth
    prometheus: PromHealth | None
    dlq_length: int | None


class PlatformDashboardService:
    def __init__(
        self,
        *,
        queries: PlatformQueries,
        health: HealthSource,
        dlq_length: Callable[[], Awaitable[int]],
        clock: Clock,
    ) -> None:
        self._q = queries
        self._health = health
        self._dlq_length = dlq_length
        self._clock = clock

    async def build(self) -> PlatformDashboard:
        now = self._clock.now()
        tenants = await self._q.tenants(now)
        clients: dict[str, int] = defaultdict(int)
        by_plan: dict[str, int] = defaultdict(int)
        attention: list[AttentionItem] = []
        for t in tenants:
            clients[t.status] += 1
            by_plan[t.plan_code] += 1
            reasons = attention_reasons(
                status=t.status,
                trial_ends_at=t.trial_ends_at,
                paid_until=t.paid_until,
                cameras_offline=t.cameras_offline,
                sessions_7d=t.sessions_7d,
                lots=t.lots,
                now=now,
            )
            if reasons:
                attention.append(
                    AttentionItem(t.tenant_id, t.name, t.status, t.plan_code, reasons, t.trial_ends_at, t.paid_until)
                )
        paying = [t.price for t in tenants if t.status == "ACTIVE"]
        revenue, contract = mrr(paying)
        weeks = 8
        since = now - timedelta(weeks=weeks)
        prom = await self._health.snapshot()
        try:
            dlq: int | None = await self._dlq_length()
        except Exception:  # Redis недоступен — дашборд всё равно открывается
            dlq = None
        trials = [t for t in tenants if t.status == "TRIAL"]
        return PlatformDashboard(
            generated_at=now,
            clients=dict(clients),
            mrr=revenue,
            contract_clients=contract,
            trials=len(trials),
            trials_ending=sum(1 for a in attention if "TRIAL_ENDING" in a.reasons),
            signups_by_week=await self._q.signups(since),
            by_plan=dict(by_plan),
            attention=sorted(attention, key=lambda a: (-len(a.reasons), a.name))[:50],
            health_source="prometheus" if prom is not None else "local",
            local=await self._q.local_health(now),
            prometheus=prom,
            dlq_length=dlq,
        )
