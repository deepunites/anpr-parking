"""Чистые правила дашбордов: период в часовом поясе клиента, доступность камер по истории статусов, сигналы
«расхождение кассы», «много ручных открытий», «устройство недоступно», «лимит тарифа близко», MRR платформы."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from parking.shared.errors import ValidationError

MAX_PERIOD_DAYS = 92
AVAILABLE = frozenset({"ONLINE", "DEGRADED"})  # камера принимает события
KNOWN = frozenset({"ONLINE", "DEGRADED", "SILENT", "OFFLINE"})  # UNKNOWN — до первого контакта, в расчёт не идёт
MANUAL_OPEN_MIN = 5  # меньше — шум смены, не сигнал
MANUAL_OPEN_SHARE = 0.10  # доля ручных открытий от выездов
DEVICE_AVAILABILITY_MIN = 0.95
LIMIT_WARN_SHARE = 0.9
TRIAL_ENDING_DAYS = 3
QUIET_DAYS = 7


@dataclass(frozen=True, slots=True)
class Period:
    """Включительный диапазон локальных дат клиента."""

    start: date
    end: date
    tz: str

    @classmethod
    def parse(cls, start: date | None, end: date | None, tz: str, today: date, *, days: int = 7) -> Period:
        end = end or today
        start = start or end - timedelta(days=max(days, 1) - 1)
        if start > end:
            raise ValidationError("period start is after its end", code="INVALID_PERIOD")
        if (end - start).days + 1 > MAX_PERIOD_DAYS:
            raise ValidationError(f"period is longer than {MAX_PERIOD_DAYS} days", code="INVALID_PERIOD")
        return cls(start, end, tz)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def bounds(self) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.tz)
        start = datetime.combine(self.start, time.min, zone).astimezone(UTC)
        end = datetime.combine(self.end + timedelta(days=1), time.min, zone).astimezone(UTC)
        return start, end

    def previous(self) -> Period:
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1), self.tz)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]


def availability(
    initial: str | None, transitions: list[tuple[datetime, str]], start: datetime, end: datetime
) -> float | None:
    """Доля известного времени в [start, end), когда камера принимала события. None — статус не был известен ни
    минуты (новая камера без контакта). `initial` — последний статус до start; transitions — по возрастанию."""
    current, at = initial, start
    up = known = 0.0
    for ts, status in transitions:
        if ts <= start:
            current = status
            continue
        if ts >= end:
            break
        span = (ts - at).total_seconds()
        if current in KNOWN:
            known += span
            up += span if current in AVAILABLE else 0.0
        current, at = status, ts
    span = (end - at).total_seconds()
    if current in KNOWN:
        known += span
        up += span if current in AVAILABLE else 0.0
    return None if known <= 0 else round(up / known, 4)


class Severity(StrEnum):
    WARN = "warn"
    BAD = "bad"


@dataclass(frozen=True, slots=True)
class Signal:
    code: str
    severity: Severity
    params: dict[str, object] = field(default_factory=dict)


def cash_signal(discrepancies: list[int]) -> Signal | None:
    nonzero = [d for d in discrepancies if d != 0]
    if not nonzero:
        return None
    shortage = sum(d for d in nonzero if d < 0)
    return Signal(
        "CASH_DISCREPANCY",
        Severity.BAD if shortage < 0 else Severity.WARN,
        {"shifts": len(nonzero), "shortage": -shortage, "surplus": sum(d for d in nonzero if d > 0)},
    )


def manual_open_signal(manual_opens: int, exits: int, top_operator: str | None, top_count: int) -> Signal | None:
    share = manual_opens / exits if exits else 1.0
    if manual_opens < MANUAL_OPEN_MIN or share < MANUAL_OPEN_SHARE:
        return None
    return Signal(
        "MANY_MANUAL_OPENS",
        Severity.BAD if share >= 2 * MANUAL_OPEN_SHARE else Severity.WARN,
        {"opens": manual_opens, "exits": exits, "share": round(share, 3), "operator": top_operator, "top": top_count},
    )


def device_signal(names_below: list[str]) -> Signal | None:
    if not names_below:
        return None
    return Signal("DEVICE_UNAVAILABLE", Severity.WARN, {"devices": names_below[:5], "count": len(names_below)})


def limit_signals(usage: list[tuple[str, int, int | None]]) -> list[Signal]:
    out = []
    for kind, used, limit in usage:
        if limit and used >= limit * LIMIT_WARN_SHARE:
            out.append(
                Signal(
                    "LIMIT_NEAR",
                    Severity.BAD if used >= limit else Severity.WARN,
                    {"kind": kind, "used": used, "limit": limit},
                )
            )
    return out


def mrr(active_prices: list[int | None]) -> tuple[int, int]:
    """(MRR по прайсу в минорных единицах, число клиентов «по договору» — тариф без цены)."""
    return sum(p for p in active_prices if p is not None), sum(1 for p in active_prices if p is None)


def attention_reasons(
    *,
    status: str,
    trial_ends_at: datetime | None,
    paid_until: datetime | None,
    cameras_offline: int,
    sessions_7d: int,
    lots: int,
    now: datetime,
) -> list[str]:
    """Почему владельцу платформы стоит посмотреть на клиента."""
    reasons: list[str] = []
    if status == "SUSPENDED":
        reasons.append("SUSPENDED")
    if status == "TRIAL" and trial_ends_at is not None:
        if trial_ends_at <= now:
            reasons.append("TRIAL_EXPIRED")
        elif trial_ends_at - now <= timedelta(days=TRIAL_ENDING_DAYS):
            reasons.append("TRIAL_ENDING")
    if status == "ACTIVE" and paid_until is not None and paid_until < now:
        reasons.append("PAYMENT_OVERDUE")
    if cameras_offline > 0:
        reasons.append("CAMERAS_OFFLINE")
    if status != "SUSPENDED" and lots > 0 and sessions_7d == 0:
        reasons.append("NO_TRAFFIC")
    return reasons
