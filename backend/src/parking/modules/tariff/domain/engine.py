"""Чистая функция расчёта стоимости (I-7): без I/O, детерминирована, неотрицательна, монотонна по длительности."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

from parking.modules.tariff.domain.rules import DayTerms, RoundingMode, TariffRules
from parking.shared.clock import ensure_aware
from parking.shared.errors import ValidationError
from parking.shared.money import Money, Rounding

FIRST_HOUR_MINUTES = 60


class AccessKind(enum.StrEnum):
    NORMAL = "NORMAL"
    VIP = "VIP"
    SUBSCRIPTION = "SUBSCRIPTION"
    WHITELIST = "WHITELIST"
    BLACKLIST = "BLACKLIST"


@dataclass(frozen=True, slots=True)
class AccessTerms:
    """Что тарифу нужно знать о решении доступа: вид и скидка.

    BLACKLIST считается как NORMAL — решение об открытии принимается не здесь.
    """

    kind: AccessKind = AccessKind.NORMAL
    discount_percent: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.discount_percent <= 100:
            raise ValidationError("discount_percent must be within 0..100")


@dataclass(frozen=True, slots=True)
class HolidayCalendar:
    days: frozenset[date] = frozenset()


@dataclass(frozen=True, slots=True)
class BreakdownLine:
    kind: str  # FIRST_HOUR | HOURLY | OVERNIGHT | FREE_DAY | DAILY_MAX | DISCOUNT | GRACE | PRIVILEGE
    starts_at: datetime
    ends_at: datetime
    minutes: int
    amount: Money
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "from": self.starts_at.isoformat(),
            "to": self.ends_at.isoformat(),
            "minutes": self.minutes,
            "amount": self.amount.minor,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class TariffCalculation:
    amount: Money
    billing_minutes: int
    applied_rules: tuple[str, ...]
    breakdown: tuple[BreakdownLine, ...]
    gross: Money
    discount: Money
    rounding_mode: RoundingMode
    rounding_step_minutes: int
    tariff_plan_version_id: Any = None  # проставляет application-слой (UUID); домен версии не знает

    def to_snapshot(self) -> dict[str, Any]:
        """Снимок для payment.calculation_snapshot (I-7): всё, что нужно, чтобы объяснить сумму через год."""
        return {
            "amount": self.amount.minor,
            "gross": self.gross.minor,
            "discount": self.discount.minor,
            "currency": self.amount.currency.value,
            "billing_minutes": self.billing_minutes,
            "applied_rules": list(self.applied_rules),
            "rounding": {"mode": self.rounding_mode.value, "step_minutes": self.rounding_step_minutes},
            "breakdown": [line.to_json() for line in self.breakdown],
            "tariff_plan_version_id": str(self.tariff_plan_version_id) if self.tariff_plan_version_id else None,
        }


@dataclass
class _DayAccumulator:
    terms: DayTerms
    cost: Fraction = Fraction(0)
    lines: list[BreakdownLine] = field(default_factory=list)


def _exact_minutes(delta: timedelta) -> Fraction:
    """Длительность в минутах как точная дробь (микросекунды). Усечение до секунд на границах сегментов
    (полночь, ночное окно) теряло до 1 с на каждую границу — сумма сегментов не сходилась с целым (I-7/I-13)."""
    return Fraction(delta // timedelta(microseconds=1), 60_000_000)


def round_minutes(raw_minutes: int, step: int, mode: RoundingMode) -> int:
    """Округление длительности до шага; после грейса оплачивается минимум один шаг."""
    units = Fraction(raw_minutes, step)
    if mode is RoundingMode.UP:
        n = math.ceil(units)
    elif mode is RoundingMode.DOWN:
        n = math.floor(units)
    else:
        n = math.floor(units + Fraction(1, 2))
    return max(n, 1) * step


def calculate(  # noqa: PLR0915 — алгоритм намеренно линейный, читается сверху вниз
    entry: datetime,
    exit: datetime,
    rules: TariffRules,
    access: AccessTerms,
    calendar: HolidayCalendar,
    tz: ZoneInfo,
) -> TariffCalculation:
    """Стоимость стоянки за [entry, exit). Бросает ValidationError, если exit <= entry или даты naive."""
    entry, exit = ensure_aware(entry), ensure_aware(exit)
    if exit <= entry:
        raise ValidationError("exit must be after entry")
    raw_minutes = math.ceil(_exact_minutes(exit - entry))
    zero = Money.zero()
    mode, step = rules.rounding_mode, rules.rounding_step_minutes

    if access.kind in (AccessKind.VIP, AccessKind.SUBSCRIPTION):
        line = BreakdownLine("PRIVILEGE", entry, exit, raw_minutes, zero, access.kind.value)
        return TariffCalculation(zero, 0, (access.kind.value,), (line,), zero, zero, mode, step)
    if raw_minutes <= rules.grace_minutes:
        line = BreakdownLine("GRACE", entry, exit, raw_minutes, zero, f"within {rules.grace_minutes} min grace")
        return TariffCalculation(zero, 0, ("GRACE",), (line,), zero, zero, mode, step)

    billing_minutes = round_minutes(raw_minutes, rules.rounding_step_minutes, rules.rounding_mode)
    billing_end = entry + timedelta(minutes=billing_minutes)
    holidays = calendar.days
    days: dict[date, _DayAccumulator] = {}
    applied: list[str] = []

    def day_of(at: datetime) -> _DayAccumulator:
        local_day = at.astimezone(tz).date()
        acc = days.get(local_day)
        if acc is None:
            acc = days[local_day] = _DayAccumulator(rules.terms_for(local_day, holidays))
            if acc.terms.source != "BASE":
                applied.append(acc.terms.source)
        return acc

    # 1. первый час — блок с фиксированной ценой (по условиям дня въезда)
    first_minutes = min(FIRST_HOUR_MINUTES, billing_minutes)
    first_end = entry + timedelta(minutes=first_minutes)
    entry_day = day_of(entry)
    first_amount = zero if entry_day.terms.free else entry_day.terms.first_hour_price
    entry_day.cost += Fraction(first_amount.minor)
    entry_day.lines.append(
        BreakdownLine(
            "FREE_DAY" if entry_day.terms.free else "FIRST_HOUR",
            entry,
            first_end,
            first_minutes,
            first_amount,
            entry_day.terms.source,
        )
    )
    applied.append("FIRST_HOUR")

    # 2. остаток — посегментно: границы = локальная полночь, границы ночного окна, конец оплачиваемого интервала
    cursor = first_end
    while cursor < billing_end:
        acc = day_of(cursor)
        local = cursor.astimezone(tz)
        seg_end = min(_next_boundary(local, rules, tz), billing_end)
        minutes = _exact_minutes(seg_end - cursor)  # точно, без усечения до секунд: сегменты суммируются в целое
        night = rules.overnight is not None and rules.overnight.contains(local.timetz().replace(tzinfo=None))
        if acc.terms.free:
            kind, rate = "FREE_DAY", zero
        elif night and rules.overnight is not None:
            kind, rate = "OVERNIGHT", rules.overnight.price_per_hour
        else:
            kind, rate = "HOURLY", acc.terms.price_per_hour
        cost = Fraction(rate.minor) * minutes / 60
        acc.cost += cost
        acc.lines.append(
            BreakdownLine(
                kind,
                cursor,
                seg_end,
                math.ceil(minutes),
                Money.from_fraction(cost, Rounding.HALF_UP),
                f"{rate.minor}/h {acc.terms.source}",
            )
        )
        if kind == "OVERNIGHT" and "OVERNIGHT" not in applied:
            applied.append("OVERNIGHT")
        cursor = seg_end

    # 3. дневной максимум — по локальному дню; округление прорейтов — один раз в день, HALF_UP
    gross = zero
    lines: list[BreakdownLine] = []
    for day, acc in sorted(days.items()):
        day_amount = Money.from_fraction(acc.cost, Rounding.HALF_UP)
        lines.extend(acc.lines)
        if acc.terms.daily_max is not None and day_amount > acc.terms.daily_max:
            lines.append(
                BreakdownLine(
                    "DAILY_MAX",
                    acc.lines[0].starts_at,
                    acc.lines[-1].ends_at,
                    0,
                    acc.terms.daily_max - day_amount,
                    f"cap {day.isoformat()}",
                )
            )
            day_amount = acc.terms.daily_max
            if "DAILY_MAX" not in applied:
                applied.append("DAILY_MAX")
        gross += day_amount

    # 4. скидка whitelist — от общей суммы, HALF_UP; net + discount == gross всегда
    discount = zero
    if access.kind is AccessKind.WHITELIST and access.discount_percent:
        discount = gross.percent(access.discount_percent, Rounding.HALF_UP)
        lines.append(BreakdownLine("DISCOUNT", entry, exit, 0, -discount, f"WHITELIST {access.discount_percent}%"))
        applied.append(f"WHITELIST_DISCOUNT_{access.discount_percent}")
    net = gross - discount
    return TariffCalculation(net, billing_minutes, tuple(applied), tuple(lines), gross, discount, mode, step)


def _next_boundary(local: datetime, rules: TariffRules, tz: ZoneInfo) -> datetime:
    """Ближайшая граница после local: полночь или край ночного окна."""
    candidates = [datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=tz)]
    if rules.overnight is not None:
        for edge in (rules.overnight.start, rules.overnight.end):
            same_day = datetime.combine(local.date(), edge, tzinfo=tz)
            candidates.append(same_day if same_day > local else same_day + timedelta(days=1))
    return min(candidates)
