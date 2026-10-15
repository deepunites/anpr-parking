"""tariff/domain — контракт разбивки, snapshot'а и правил (фаза 8 §1, mutation testing): каждая строка breakdown,
каждое поле snapshot, сообщения валидации, границы daily_max/скидки/ночного окна, локальная дата по TZ."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from itertools import pairwise
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from parking.modules.tariff.domain.engine import (
    AccessKind,
    AccessTerms,
    BreakdownLine,
    HolidayCalendar,
    TariffCalculation,
    _exact_minutes,
    calculate,
    round_minutes,
)
from parking.modules.tariff.domain.rules import DayOverride, OvernightWindow, RoundingMode, TariffRules, _int, _override
from parking.shared.errors import ValidationError
from parking.shared.money import Money
from tests.tariff.test_engine import FIRST, HOURLY, MONDAY, TZ, at, rules

NO_HOLIDAYS = HolidayCalendar(frozenset())


def calc(
    entry: datetime,
    exit: datetime,
    r: TariffRules | None = None,
    access: AccessTerms | None = None,
    holidays: frozenset[date] = frozenset(),
) -> TariffCalculation:
    return calculate(entry, exit, r or rules(), access or AccessTerms(), HolidayCalendar(holidays), TZ)


# ------------------------------------------------------------------ ранние выходы: PRIVILEGE / GRACE


@pytest.mark.parametrize("kind", [AccessKind.VIP, AccessKind.SUBSCRIPTION])
def test_privilege_breakdown_line_is_fully_described(kind: AccessKind) -> None:
    entry, exit = MONDAY, MONDAY + timedelta(minutes=95)
    result = calc(entry, exit, access=AccessTerms(kind=kind))
    assert result.amount == Money.zero() and result.gross == Money.zero() and result.discount == Money.zero()
    assert result.billing_minutes == 0 and result.applied_rules == (kind.value,)
    assert result.rounding_mode is RoundingMode.UP and result.rounding_step_minutes == 60
    assert len(result.breakdown) == 1
    line = result.breakdown[0]
    assert line == BreakdownLine("PRIVILEGE", entry, exit, 95, Money.zero(), kind.value)
    assert line.to_json() == {
        "kind": "PRIVILEGE",
        "from": entry.isoformat(),
        "to": exit.isoformat(),
        "minutes": 95,
        "amount": 0,
        "note": kind.value,
    }


def test_grace_breakdown_line_is_fully_described() -> None:
    entry, exit = MONDAY, MONDAY + timedelta(minutes=15)  # ровно грейс
    result = calc(entry, exit)
    assert result.amount == Money.zero() and result.billing_minutes == 0
    assert result.applied_rules == ("GRACE",)
    assert result.breakdown == (BreakdownLine("GRACE", entry, exit, 15, Money.zero(), "within 15 min grace"),)
    assert result.rounding_mode is RoundingMode.UP and result.rounding_step_minutes == 60
    snapshot = result.to_snapshot()
    assert snapshot["rounding"] == {"mode": "UP", "step_minutes": 60}
    assert snapshot["breakdown"][0]["note"] == "within 15 min grace"


def test_validation_messages() -> None:
    with pytest.raises(ValidationError, match="exit must be after entry"):
        calc(MONDAY, MONDAY)
    with pytest.raises(ValidationError, match=r"discount_percent must be within 0\.\.100"):
        AccessTerms(kind=AccessKind.WHITELIST, discount_percent=101)
    with pytest.raises(ValidationError, match=r"discount_percent must be within 0\.\.100"):
        AccessTerms(kind=AccessKind.WHITELIST, discount_percent=-1)
    AccessTerms(kind=AccessKind.WHITELIST, discount_percent=100)  # границы включительно
    AccessTerms(kind=AccessKind.WHITELIST, discount_percent=0)
    with pytest.raises(ValidationError, match=r"grace_minutes must be 0\.\.1440 and rounding_step_minutes 1\.\.1440"):
        rules(grace_minutes=-1)
    with pytest.raises(ValidationError, match=r"grace_minutes must be 0\.\.1440 and rounding_step_minutes 1\.\.1440"):
        rules(rounding_step_minutes=0)
    with pytest.raises(ValidationError, match="tariff prices must be non-negative"):
        rules(daily_max=Money(-1))
    with pytest.raises(ValidationError, match="tariff prices must be non-negative"):
        rules(price_per_hour=Money(-1))
    with pytest.raises(ValidationError, match=r"weekday_overrides keys must be 0\.\.6"):
        rules(weekday_overrides={7: DayOverride(free=True)})
    rules(weekday_overrides={0: DayOverride(), 6: DayOverride()})


# ------------------------------------------------------------------ обычный расчёт: строки, applied, snapshot


def test_first_hour_and_hourly_lines_with_notes_and_sources() -> None:
    entry = MONDAY
    result = calc(entry, entry + timedelta(hours=2, minutes=30))
    assert result.billing_minutes == 180 and result.applied_rules == ("FIRST_HOUR",)
    first, hourly = result.breakdown
    assert first == BreakdownLine("FIRST_HOUR", entry, entry + timedelta(hours=1), 60, FIRST, "BASE")
    assert hourly.kind == "HOURLY" and hourly.starts_at == entry + timedelta(hours=1)
    assert hourly.ends_at == entry + timedelta(hours=3) and hourly.minutes == 120
    assert hourly.amount == HOURLY * 2 and hourly.note == f"{HOURLY.minor}/h BASE"
    assert result.gross == FIRST + HOURLY * 2 and result.amount == result.gross
    snapshot = result.to_snapshot()
    assert snapshot["amount"] == result.amount.minor and snapshot["gross"] == result.gross.minor
    assert snapshot["discount"] == 0 and snapshot["currency"] == "UZS" and snapshot["billing_minutes"] == 180
    assert snapshot["applied_rules"] == ["FIRST_HOUR"] and snapshot["tariff_plan_version_id"] is None
    assert [line["kind"] for line in snapshot["breakdown"]] == ["FIRST_HOUR", "HOURLY"]
    version = uuid4()
    assert replace(result, tariff_plan_version_id=version).to_snapshot()["tariff_plan_version_id"] == str(version)


def test_weekday_and_holiday_sources_and_free_day_lines() -> None:
    sunday = at(2026, 9, 20, 10)  # воскресенье
    r = rules(weekday_overrides={6: DayOverride(free=True)}, holiday_override=DayOverride(price_per_hour=Money(100000)))
    free = calc(sunday, sunday + timedelta(hours=3), r)
    assert free.amount == Money.zero() and free.applied_rules == ("WEEKDAY_6", "FIRST_HOUR")
    assert [line.kind for line in free.breakdown] == ["FREE_DAY", "FREE_DAY"]
    assert free.breakdown[0].note == "WEEKDAY_6" and free.breakdown[1].note == "0/h WEEKDAY_6"
    holiday = calc(sunday, sunday + timedelta(hours=3), r, holidays=frozenset({sunday.date()}))
    assert holiday.applied_rules == ("HOLIDAY", "FIRST_HOUR"), "праздник приоритетнее дня недели"
    assert holiday.breakdown[1].note == "100000/h HOLIDAY" and holiday.breakdown[1].amount == Money(200000)
    assert holiday.breakdown[0].amount == FIRST, "первый час праздника — базовый, override только на почасовую"
    weekday_terms = r.terms_for(sunday.date(), frozenset())
    assert (weekday_terms.free, weekday_terms.source, weekday_terms.price_per_hour) == (True, "WEEKDAY_6", HOURLY)
    base_terms = r.terms_for(MONDAY.date(), frozenset())
    assert base_terms == r.terms_for(MONDAY.date(), frozenset({sunday.date()}))
    assert (base_terms.source, base_terms.free, base_terms.daily_max) == ("BASE", False, None)
    holiday_terms = r.terms_for(sunday.date(), frozenset({sunday.date()}))
    assert (
        holiday_terms.source == "HOLIDAY"
        and holiday_terms.first_hour_price == FIRST
        and holiday_terms.daily_max is None
    )
    capped = rules(
        daily_max=Money(10), weekday_overrides={0: DayOverride(daily_max=Money(20), first_hour_price=Money(5))}
    )
    monday_terms = capped.terms_for(MONDAY.date(), frozenset())
    assert (monday_terms.daily_max, monday_terms.first_hour_price, monday_terms.price_per_hour) == (
        Money(20),
        Money(5),
        HOURLY,
    )
    assert capped.terms_for(sunday.date(), frozenset()).daily_max == Money(10)


def test_local_day_is_taken_in_lot_timezone_not_utc() -> None:
    """22:00 UTC = 03:00 следующего дня в Ташкенте: день недели и его override берутся по локальной дате."""
    saturday_late_utc = datetime(2026, 9, 19, 22, 0, tzinfo=UTC)  # суббота по UTC, воскресенье в Ташкенте
    r = rules(weekday_overrides={6: DayOverride(free=True)})
    result = calc(saturday_late_utc, saturday_late_utc + timedelta(hours=2), r)
    assert result.amount == Money.zero() and "WEEKDAY_6" in result.applied_rules
    in_utc = calculate(
        saturday_late_utc, saturday_late_utc + timedelta(hours=2), r, AccessTerms(), NO_HOLIDAYS, ZoneInfo("UTC")
    )
    assert in_utc.amount == FIRST + HOURLY, "в UTC это ещё суббота — платно"


def test_daily_max_boundary_and_cap_line() -> None:
    r = rules(daily_max=FIRST + HOURLY * 2)  # ровно 3 часа = максимум
    exact = calc(MONDAY, MONDAY + timedelta(hours=3), r)
    assert exact.amount == FIRST + HOURLY * 2 and "DAILY_MAX" not in exact.applied_rules
    over = calc(MONDAY, MONDAY + timedelta(hours=5), r)
    assert over.amount == FIRST + HOURLY * 2 and over.applied_rules == ("FIRST_HOUR", "DAILY_MAX")
    cap = over.breakdown[-1]
    assert cap.kind == "DAILY_MAX" and cap.minutes == 0 and cap.amount == -(HOURLY * 2)
    assert cap.note == f"cap {MONDAY.date().isoformat()}" and cap.starts_at == MONDAY
    assert cap.ends_at == MONDAY + timedelta(hours=5)


def test_overnight_window_applied_once_and_line_kind() -> None:
    r = rules(overnight=OvernightWindow(time(22, 0), time(6, 0), Money(100000)))
    entry = at(2026, 9, 14, 21)
    result = calc(entry, entry + timedelta(hours=4))  # без ночного окна — обычный расчёт
    assert result.applied_rules == ("FIRST_HOUR",)
    night = calc(entry, entry + timedelta(hours=4), r)
    assert night.applied_rules == ("FIRST_HOUR", "OVERNIGHT"), "OVERNIGHT добавляется один раз"
    kinds = [line.kind for line in night.breakdown]
    assert kinds == ["FIRST_HOUR", "OVERNIGHT", "OVERNIGHT"], "ночное окно режется локальной полночью"
    assert night.breakdown[1].note == "100000/h BASE" and night.breakdown[2].note == "100000/h BASE"
    assert night.breakdown[1].starts_at == at(2026, 9, 14, 22) and night.breakdown[1].minutes == 120
    assert night.breakdown[2].starts_at == at(2026, 9, 15, 0) and night.breakdown[2].minutes == 60
    assert night.amount == FIRST + Money(300000)
    window = r.overnight
    assert window is not None
    assert window.contains(time(22, 0)) and window.contains(time(23, 59)) and window.contains(time(5, 59))
    assert not window.contains(time(6, 0)) and not window.contains(time(21, 59))
    day_window = OvernightWindow(time(1, 0), time(3, 0), Money(1))
    assert day_window.contains(time(1, 0)) and day_window.contains(time(2, 59))
    assert not day_window.contains(time(3, 0)) and not day_window.contains(time(0, 59))


def test_discount_line_and_zero_discount_whitelist() -> None:
    result = calc(
        MONDAY, MONDAY + timedelta(hours=2), access=AccessTerms(kind=AccessKind.WHITELIST, discount_percent=33)
    )
    gross = FIRST + HOURLY
    assert result.gross == gross and result.discount == gross.percent(
        33, __import__("parking.shared.money", fromlist=["Rounding"]).Rounding.HALF_UP
    )
    assert result.amount + result.discount == result.gross
    line = result.breakdown[-1]
    assert line.kind == "DISCOUNT" and line.amount == -result.discount and line.note == "WHITELIST 33%"
    assert line.starts_at == MONDAY and line.ends_at == MONDAY + timedelta(hours=2) and line.minutes == 0
    assert result.applied_rules == ("FIRST_HOUR", "WHITELIST_DISCOUNT_33")
    zero = calc(MONDAY, MONDAY + timedelta(hours=2), access=AccessTerms(kind=AccessKind.WHITELIST, discount_percent=0))
    assert zero.discount == Money.zero() and "DISCOUNT" not in [line.kind for line in zero.breakdown]
    assert zero.applied_rules == ("FIRST_HOUR",)
    normal = calc(MONDAY, MONDAY + timedelta(hours=2), access=AccessTerms(kind=AccessKind.NORMAL, discount_percent=0))
    assert normal.discount == Money.zero()


def test_segments_end_exactly_at_billing_end_and_sum_to_whole() -> None:
    entry = MONDAY + timedelta(seconds=47, microseconds=834305)
    result = calc(entry, entry + timedelta(hours=2), rules(grace_minutes=0, rounding_step_minutes=1))
    assert result.breakdown[-1].ends_at == entry + timedelta(hours=2)
    assert sum((line.amount for line in result.breakdown), Money.zero()) == result.gross == FIRST + HOURLY
    assert result.billing_minutes == 120


def test_exact_minutes_and_round_minutes() -> None:
    assert _exact_minutes(timedelta(minutes=1)) == Fraction(1)
    assert _exact_minutes(timedelta(seconds=30)) == Fraction(1, 2)
    assert _exact_minutes(timedelta(microseconds=1)) == Fraction(1, 60_000_000)
    assert round_minutes(61, 60, RoundingMode.UP) == 120 and round_minutes(60, 60, RoundingMode.UP) == 60
    assert round_minutes(119, 60, RoundingMode.DOWN) == 60 and round_minutes(1, 60, RoundingMode.DOWN) == 60
    assert round_minutes(89, 60, RoundingMode.NEAREST) == 60 and round_minutes(90, 60, RoundingMode.NEAREST) == 120
    assert round_minutes(7, 15, RoundingMode.NEAREST) == 15


def test_midnight_boundary_segments() -> None:
    entry = at(2026, 9, 14, 23, 30)
    result = calc(
        entry,
        entry + timedelta(hours=2),
        rules(grace_minutes=0, weekday_overrides={1: DayOverride(price_per_hour=Money(100000))}),
    )
    kinds = [(line.kind, line.starts_at, line.ends_at) for line in result.breakdown]
    assert kinds == [
        ("FIRST_HOUR", entry, at(2026, 9, 15, 0, 30)),
        ("HOURLY", at(2026, 9, 15, 0, 30), at(2026, 9, 15, 1, 30)),
    ]
    assert result.breakdown[1].note == "100000/h WEEKDAY_1" and result.applied_rules == ("FIRST_HOUR", "WEEKDAY_1")
    late = calc(
        at(2026, 9, 14, 22, 30),
        at(2026, 9, 15, 1, 30),
        rules(grace_minutes=0, weekday_overrides={1: DayOverride(price_per_hour=Money(100000))}),
    )
    assert [line.kind for line in late.breakdown] == ["FIRST_HOUR", "HOURLY", "HOURLY"]
    assert late.breakdown[1].ends_at == at(2026, 9, 15, 0, 0) and late.breakdown[2].starts_at == at(2026, 9, 15, 0, 0)
    assert late.breakdown[1].amount == Money(150000) and late.breakdown[2].amount == Money(150000)


# ------------------------------------------------------------------ JSON-контракт правил


def test_rules_json_contract_every_field() -> None:
    r = rules(
        daily_max=Money(700000),
        overnight=OvernightWindow(time(22, 0), time(6, 0), Money(100000)),
        weekday_overrides={
            6: DayOverride(free=True),
            5: DayOverride(price_per_hour=Money(200000), first_hour_price=Money(300000), daily_max=Money(500000)),
        },
        holiday_override=DayOverride(free=True),
        first_hour_prorated=True,
        amount_step=Money(100000),
    )
    data = r.to_json()
    assert data == {
        "grace_minutes": 15,
        "first_hour_price": FIRST.minor,
        "price_per_hour": HOURLY.minor,
        "rounding_mode": "UP",
        "rounding_step_minutes": 60,
        "daily_max": 700000,
        "overnight": {"start": "22:00", "end": "06:00", "price_per_hour": 100000},
        "weekday_overrides": {
            "6": {"price_per_hour": None, "first_hour_price": None, "daily_max": None, "free": True},
            "5": {"price_per_hour": 200000, "first_hour_price": 300000, "daily_max": 500000, "free": False},
        },
        "holiday": {"price_per_hour": None, "first_hour_price": None, "daily_max": None, "free": True},
        "first_hour_prorated": True,
        "amount_step": 100000,
    }
    assert TariffRules.from_json(data) == r
    old = {k: v for k, v in data.items() if k not in ("first_hour_prorated", "amount_step")}
    assert TariffRules.from_json(old) == replace(r, first_hour_prorated=False, amount_step=None), "старые версии"
    minimal = rules().to_json()
    assert minimal["daily_max"] is None and minimal["overnight"] is None
    assert minimal["weekday_overrides"] is None and minimal["holiday"] is None
    assert TariffRules.from_json(minimal) == rules()


@pytest.mark.parametrize(
    ("broken", "fragment"),
    [
        ({"grace_minutes": "15"}, "expected integer, got str"),
        ({"grace_minutes": True}, "expected integer, got bool"),
        ({"first_hour_price": 1.5}, "expected integer, got float"),
        ({"rounding_mode": "SIDEWAYS"}, "invalid tariff rules"),
        ({"overnight": {"start": "22:00"}}, "end"),
        ({"weekday_overrides": {"x": {}}}, "invalid tariff rules"),
        ({"holiday": {"price_per_hour": "a"}}, "expected integer"),
        ({"daily_max": "1"}, "expected integer"),
    ],
)
def test_rules_from_json_rejects_bad_input(broken: dict[str, object], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        TariffRules.from_json({**rules().to_json(), **broken})


def test_override_parsing_defaults() -> None:
    assert _override({}) == DayOverride()
    assert _override({"free": 1}) == DayOverride(free=True)
    assert _override({"price_per_hour": 5, "first_hour_price": None, "daily_max": 7}) == DayOverride(
        price_per_hour=Money(5), daily_max=Money(7)
    )
    assert _int(5) == 5
    for bad in ("5", 5.0, True, None):
        with pytest.raises(ValueError, match="expected integer"):
            _int(bad)


def test_per_minute_first_hour_rounded_to_thousand() -> None:
    """MediaPark (2026-10-15): «на 16-й минуте считает целый час» — поминутно от часовой цены (5 000 первый час,
    8 000 дальше), итог к ближайшей 1 000 сум: без мелочи на сдачу. 15 минут — бесплатно."""
    r = rules(
        first_hour_price=Money(500000),
        price_per_hour=Money(800000),
        rounding_step_minutes=1,
        first_hour_prorated=True,
        amount_step=Money(100000),
    )
    entry = at(2026, 10, 15, 10, 0)
    sums = {
        m: calc(entry, entry + timedelta(minutes=m), r).amount.minor // 100 for m in (15, 16, 18, 30, 60, 61, 90, 120)
    }
    assert sums == {15: 0, 16: 1000, 18: 2000, 30: 3000, 60: 5000, 61: 5000, 90: 9000, 120: 13000}
    seq = [calc(entry, entry + timedelta(minutes=m), r).amount for m in range(16, 24 * 60)]
    assert all(a <= b for a, b in pairwise(seq)), "монотонно по длительности (I-7)"
    result = calc(entry, entry + timedelta(minutes=16), r)
    assert [line.kind for line in result.breakdown] == ["FIRST_HOUR", "ROUNDING"]
    assert result.breakdown[1].amount == Money(100000) - result.breakdown[0].amount
    with pytest.raises(ValidationError):
        TariffRules.from_json({**r.to_json(), "first_hour_prorated": "yes"})
    with pytest.raises(ValidationError):
        rules(amount_step=Money(0))
