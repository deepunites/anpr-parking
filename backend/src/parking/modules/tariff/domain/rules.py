"""Правила тарифа — value objects, разобранные из JSONB `tariff_plan_version.rules`.

Семантика (см. docs/TARIFF.md):
  grace_minutes          — длительность ≤ грейса: платить нечего; иначе оплачивается ВСЁ время с въезда
                           (грейс не вычитается)
  first_hour_price       — первые 60 оплачиваемых минут — блок с фиксированной ценой
  price_per_hour         — далее поминутный прорейт от часовой цены
  rounding               — длительность округляется до шага (UP | DOWN | NEAREST), минимум один шаг после грейса
  daily_max              — потолок за локальный календарный день площадки
  overnight              — окно (может пересекать полночь), внутри которого действует своя часовая цена
  weekday_overrides      — переопределения по дню недели (0 = понедельник … 6 = воскресенье)
  holiday                — переопределение для дат праздничного календаря
Все суммы — Money в тийинах.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from parking.shared.errors import ValidationError
from parking.shared.money import Money


class RoundingMode(enum.StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NEAREST = "NEAREST"


@dataclass(frozen=True, slots=True)
class OvernightWindow:
    start: time
    end: time
    price_per_hour: Money

    def contains(self, at: time) -> bool:
        if self.start < self.end:
            return self.start <= at < self.end
        return at >= self.start or at < self.end


@dataclass(frozen=True, slots=True)
class DayOverride:
    """Переопределение для дня недели или праздника. None — взять базовое значение."""

    price_per_hour: Money | None = None
    first_hour_price: Money | None = None
    daily_max: Money | None = None
    free: bool = False


@dataclass(frozen=True, slots=True)
class DayTerms:
    """Действующие условия конкретного дня после применения переопределений."""

    price_per_hour: Money
    first_hour_price: Money
    daily_max: Money | None
    free: bool
    source: str  # BASE | WEEKDAY_n | HOLIDAY


@dataclass(frozen=True, slots=True)
class TariffRules:
    grace_minutes: int
    first_hour_price: Money
    price_per_hour: Money
    rounding_mode: RoundingMode
    rounding_step_minutes: int
    daily_max: Money | None = None
    overnight: OvernightWindow | None = None
    weekday_overrides: dict[int, DayOverride] = field(default_factory=dict)
    holiday_override: DayOverride | None = None

    def __post_init__(self) -> None:
        if self.grace_minutes < 0 or self.rounding_step_minutes < 1:
            raise ValidationError("grace_minutes must be >= 0 and rounding_step_minutes >= 1")
        for m in (self.first_hour_price, self.price_per_hour, *(x for x in [self.daily_max] if x is not None)):
            if m.is_negative:
                raise ValidationError("tariff prices must be non-negative")
        if any(k not in range(7) for k in self.weekday_overrides):
            raise ValidationError("weekday_overrides keys must be 0..6")

    def terms_for(self, day: date, holidays: frozenset[date]) -> DayTerms:
        override = None
        source = "BASE"
        if day in holidays and self.holiday_override is not None:
            override, source = self.holiday_override, "HOLIDAY"
        elif day.weekday() in self.weekday_overrides:
            override, source = self.weekday_overrides[day.weekday()], f"WEEKDAY_{day.weekday()}"
        if override is None:
            return DayTerms(self.price_per_hour, self.first_hour_price, self.daily_max, False, source)
        return DayTerms(
            price_per_hour=override.price_per_hour if override.price_per_hour is not None else self.price_per_hour,
            first_hour_price=override.first_hour_price
            if override.first_hour_price is not None
            else self.first_hour_price,
            daily_max=override.daily_max if override.daily_max is not None else self.daily_max,
            free=override.free,
            source=source,
        )

    # --- JSON (контракт с tariff_plan_version.rules, проверяется и app.tariff_rules_valid) ---
    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> TariffRules:
        try:
            overnight = None
            if raw.get("overnight") is not None:
                o = raw["overnight"]
                overnight = OvernightWindow(
                    time.fromisoformat(o["start"]), time.fromisoformat(o["end"]), _money(o["price_per_hour"])
                )
            overrides = {int(k): _override(v) for k, v in (raw.get("weekday_overrides") or {}).items()}
            holiday = _override(raw["holiday"]) if raw.get("holiday") is not None else None
            return cls(
                grace_minutes=_int(raw["grace_minutes"]),
                first_hour_price=_money(raw["first_hour_price"]),
                price_per_hour=_money(raw["price_per_hour"]),
                rounding_mode=RoundingMode(raw["rounding_mode"]),
                rounding_step_minutes=_int(raw["rounding_step_minutes"]),
                daily_max=_money(raw["daily_max"]) if raw.get("daily_max") is not None else None,
                overnight=overnight,
                weekday_overrides=overrides,
                holiday_override=holiday,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"invalid tariff rules: {exc}") from None

    def to_json(self) -> dict[str, Any]:
        return {
            "grace_minutes": self.grace_minutes,
            "first_hour_price": self.first_hour_price.minor,
            "price_per_hour": self.price_per_hour.minor,
            "rounding_mode": self.rounding_mode.value,
            "rounding_step_minutes": self.rounding_step_minutes,
            "daily_max": self.daily_max.minor if self.daily_max else None,
            "overnight": (
                {
                    "start": self.overnight.start.strftime("%H:%M"),
                    "end": self.overnight.end.strftime("%H:%M"),
                    "price_per_hour": self.overnight.price_per_hour.minor,
                }
                if self.overnight
                else None
            ),
            "weekday_overrides": {str(k): _override_json(v) for k, v in self.weekday_overrides.items()} or None,
            "holiday": _override_json(self.holiday_override) if self.holiday_override else None,
        }


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected integer, got {type(value).__name__}")
    return value


def _money(value: Any) -> Money:
    return Money(_int(value))


def _override(raw: dict[str, Any]) -> DayOverride:
    return DayOverride(
        price_per_hour=_money(raw["price_per_hour"]) if raw.get("price_per_hour") is not None else None,
        first_hour_price=_money(raw["first_hour_price"]) if raw.get("first_hour_price") is not None else None,
        daily_max=_money(raw["daily_max"]) if raw.get("daily_max") is not None else None,
        free=bool(raw.get("free", False)),
    )


def _override_json(o: DayOverride) -> dict[str, Any]:
    return {
        "price_per_hour": o.price_per_hour.minor if o.price_per_hour else None,
        "first_hour_price": o.first_hour_price.minor if o.first_hour_price else None,
        "daily_max": o.daily_max.minor if o.daily_max else None,
        "free": o.free,
    }
