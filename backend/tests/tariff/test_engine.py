"""Tariff engine (3.3): перечисленные случаи фазы + property-based (I-7)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from parking.modules.tariff.domain import (
    AccessKind,
    AccessTerms,
    DayOverride,
    HolidayCalendar,
    OvernightWindow,
    RoundingMode,
    TariffRules,
    calculate,
    round_minutes,
)
from parking.shared.errors import ValidationError
from parking.shared.money import Money

TZ = ZoneInfo("Asia/Tashkent")
FIRST = Money(500000)  # 5 000 сум
HOURLY = Money(300000)  # 3 000 сум/ч
NIGHT = Money(100000)


def rules(**overrides: object) -> TariffRules:
    base: dict[str, object] = {
        "grace_minutes": 15,
        "first_hour_price": FIRST,
        "price_per_hour": HOURLY,
        "rounding_mode": RoundingMode.UP,
        "rounding_step_minutes": 60,
        "daily_max": None,
        "overnight": None,
    }
    base.update(overrides)
    return TariffRules(**base)  # type: ignore[arg-type]


def at(y: int, m: int, d: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


def amount(
    entry: datetime,
    exit: datetime,
    r: TariffRules,
    access: AccessTerms | None = None,
    holidays: frozenset[date] = frozenset(),
) -> Money:
    return calculate(entry, exit, r, access or AccessTerms(), HolidayCalendar(holidays), TZ).amount


MONDAY = at(2026, 9, 14, 8)  # 14.09.2026 — понедельник


def test_exactly_grace_is_free_and_one_second_more_is_first_hour() -> None:
    r = rules()
    exact = calculate(MONDAY, MONDAY + timedelta(minutes=15), r, AccessTerms(), HolidayCalendar(), TZ)
    assert exact.amount == Money.zero() and exact.billing_minutes == 0 and exact.applied_rules == ("GRACE",)
    over = calculate(MONDAY, MONDAY + timedelta(minutes=15, seconds=1), r, AccessTerms(), HolidayCalendar(), TZ)
    assert over.amount == FIRST and over.billing_minutes == 60 and "FIRST_HOUR" in over.applied_rules


def test_hourly_after_first_hour_and_rounding_modes() -> None:
    assert amount(MONDAY, MONDAY + timedelta(hours=2), rules()) == Money(800000)
    two_thirty = MONDAY + timedelta(hours=2, minutes=30)
    assert amount(MONDAY, two_thirty, rules(rounding_mode=RoundingMode.UP)) == Money(1100000)
    assert amount(MONDAY, two_thirty, rules(rounding_mode=RoundingMode.DOWN)) == Money(800000)
    assert amount(MONDAY, two_thirty, rules(rounding_mode=RoundingMode.NEAREST)) == Money(1100000)
    assert amount(MONDAY, two_thirty, rules(rounding_mode=RoundingMode.NEAREST, rounding_step_minutes=30)) == Money(
        950000
    )
    assert round_minutes(89, 60, RoundingMode.NEAREST) == 60 and round_minutes(91, 60, RoundingMode.NEAREST) == 120
    assert round_minutes(1, 60, RoundingMode.DOWN) == 60, "после грейса оплачивается минимум один шаг"


def test_overnight_window_and_midnight_crossing() -> None:
    r = rules(overnight=OvernightWindow(time(22, 0), time(7, 0), NIGHT))
    evening = at(2026, 9, 14, 21)
    calc = calculate(evening, evening + timedelta(hours=2), r, AccessTerms(), HolidayCalendar(), TZ)
    assert calc.amount == Money(600000) and "OVERNIGHT" in calc.applied_rules
    late = at(2026, 9, 14, 23)
    crossing = calculate(late, late + timedelta(hours=2), r, AccessTerms(), HolidayCalendar(), TZ)
    assert crossing.amount == Money(600000)
    days = {
        line.starts_at.astimezone(TZ).date()
        for line in crossing.breakdown
        if line.kind in {"FIRST_HOUR", "OVERNIGHT", "HOURLY"}
    }
    assert days == {date(2026, 9, 14), date(2026, 9, 15)}
    # ночь без окна через полночь: 05:00–09:00 = первый час + 1 ч ночь (до 07:00) + 2 ч день
    dawn = at(2026, 9, 15, 5)
    assert amount(dawn, dawn + timedelta(hours=4), r) == Money(500000 + 100000 + 600000)


def test_weekday_changes_mid_session_sunday_free() -> None:
    r = rules(weekday_overrides={6: DayOverride(free=True)})
    saturday_night = at(2026, 9, 19, 23)  # суббота
    calc = calculate(saturday_night, saturday_night + timedelta(hours=3), r, AccessTerms(), HolidayCalendar(), TZ)
    assert calc.amount == FIRST and "WEEKDAY_6" in calc.applied_rules
    assert any(line.kind == "FREE_DAY" for line in calc.breakdown)
    sunday = at(2026, 9, 20, 10)
    assert amount(sunday, sunday + timedelta(hours=5), r) == Money.zero()


def test_holiday_starting_mid_session() -> None:
    r = rules(holiday_override=DayOverride(price_per_hour=Money.zero(), first_hour_price=Money.zero()))
    holidays = frozenset({date(2026, 9, 1)})
    entry = at(2026, 8, 31, 22)
    calc = calculate(entry, entry + timedelta(hours=12), r, AccessTerms(), HolidayCalendar(holidays), TZ)
    # 31.08 22:00–00:00: первый час 5000 + 1 ч × 3000; 01.09 00:00–10:00 — праздник по 0
    assert calc.amount == Money(800000) and "HOLIDAY" in calc.applied_rules
    assert amount(entry, entry + timedelta(hours=12), r, holidays=frozenset()) == Money(500000 + 11 * 300000)


def test_seven_days_with_daily_max() -> None:
    r = rules(daily_max=Money(3000000))
    calc = calculate(MONDAY, MONDAY + timedelta(days=7), r, AccessTerms(), HolidayCalendar(), TZ)
    assert calc.amount == Money(3000000 * 7 + 2400000) and "DAILY_MAX" in calc.applied_rules
    assert calc.billing_minutes == 7 * 24 * 60
    assert calc.to_snapshot()["breakdown"][-1]["kind"] in {"DAILY_MAX", "HOURLY"}


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1), timedelta(hours=-3)])
def test_zero_and_negative_intervals_raise(delta: timedelta) -> None:
    with pytest.raises(ValidationError):
        calculate(MONDAY, MONDAY + delta, rules(), AccessTerms(), HolidayCalendar(), TZ)
    with pytest.raises(ValueError, match="naive"):
        calculate(
            MONDAY.replace(tzinfo=None), MONDAY + timedelta(hours=1), rules(), AccessTerms(), HolidayCalendar(), TZ
        )


def test_privileges_and_blacklist() -> None:
    hour = MONDAY + timedelta(hours=1)
    for kind in (AccessKind.VIP, AccessKind.SUBSCRIPTION):
        calc = calculate(MONDAY, hour, rules(), AccessTerms(kind=kind), HolidayCalendar(), TZ)
        assert calc.amount == Money.zero() and calc.applied_rules == (kind.value,) and calc.billing_minutes == 0
    assert amount(MONDAY, hour, rules(), AccessTerms(kind=AccessKind.BLACKLIST)) == FIRST


def test_thirty_three_percent_discount_keeps_every_tiyin() -> None:
    r = rules(grace_minutes=0, first_hour_price=Money(1000), price_per_hour=Money(0))
    calc = calculate(
        MONDAY, MONDAY + timedelta(minutes=30), r, AccessTerms(AccessKind.WHITELIST, 33), HolidayCalendar(), TZ
    )
    assert calc.gross == Money(1000) and calc.discount == Money(330) and calc.amount == Money(670)
    assert calc.amount + calc.discount == calc.gross
    assert "WHITELIST_DISCOUNT_33" in calc.applied_rules
    odd = calculate(
        MONDAY,
        MONDAY + timedelta(minutes=30),
        rules(grace_minutes=0, first_hour_price=Money(1001), price_per_hour=Money(0)),
        AccessTerms(AccessKind.WHITELIST, 33),
        HolidayCalendar(),
        TZ,
    )
    assert odd.amount + odd.discount == Money(1001)


def test_snapshot_is_self_describing() -> None:
    calc = calculate(MONDAY, MONDAY + timedelta(hours=3), rules(), AccessTerms(), HolidayCalendar(), TZ)
    snap = calc.to_snapshot()
    assert (
        snap["amount"] == 1100000
        and snap["billing_minutes"] == 180
        and snap["rounding"] == {"mode": "UP", "step_minutes": 60}
    )
    assert sum(line["amount"] for line in snap["breakdown"]) == snap["amount"]
    assert snap["currency"] == "UZS"


def test_rules_json_roundtrip_and_validation() -> None:
    raw = {
        "grace_minutes": 10,
        "first_hour_price": 500000,
        "price_per_hour": 300000,
        "daily_max": 3000000,
        "rounding_mode": "NEAREST",
        "rounding_step_minutes": 15,
        "overnight": {"start": "22:00", "end": "07:00", "price_per_hour": 100000},
        "weekday_overrides": {"6": {"free": True}, "5": {"price_per_hour": 200000}},
        "holiday": {"free": True},
    }
    parsed = TariffRules.from_json(raw)
    assert (
        parsed.overnight is not None and parsed.overnight.price_per_hour == NIGHT and parsed.weekday_overrides[6].free
    )
    assert TariffRules.from_json(parsed.to_json()) == parsed
    for bad in (
        {},
        {**raw, "price_per_hour": 12.5},
        {**raw, "rounding_mode": "CEIL"},
        {**raw, "weekday_overrides": {"7": {}}},
        {**raw, "grace_minutes": True},
    ):
        with pytest.raises(ValidationError):
            TariffRules.from_json(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------- property-based (I-7)
money = st.integers(min_value=0, max_value=2_000_000).map(Money)
optional_money = st.one_of(st.none(), money)
overrides = st.dictionaries(
    st.integers(min_value=0, max_value=6),
    st.builds(
        DayOverride,
        price_per_hour=optional_money,
        first_hour_price=optional_money,
        daily_max=optional_money,
        free=st.booleans(),
    ),
    max_size=3,
)
windows = st.one_of(
    st.none(),
    st.builds(
        OvernightWindow,
        start=st.times().map(lambda t: t.replace(second=0, microsecond=0)),
        end=st.times().map(lambda t: t.replace(second=0, microsecond=0)),
        price_per_hour=money,
    ),
)
rule_sets = st.builds(
    TariffRules,
    grace_minutes=st.integers(min_value=0, max_value=60),
    first_hour_price=money,
    price_per_hour=money,
    rounding_mode=st.sampled_from(list(RoundingMode)),
    rounding_step_minutes=st.sampled_from([1, 5, 10, 15, 30, 60]),
    daily_max=optional_money,
    overnight=windows,
    weekday_overrides=overrides,
    holiday_override=st.one_of(st.none(), st.builds(DayOverride, free=st.booleans(), price_per_hour=optional_money)),
)
entries = st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2026, 12, 31), timezones=st.just(UTC))
access_terms = st.one_of(
    st.just(AccessTerms()),
    st.builds(
        AccessTerms, kind=st.just(AccessKind.WHITELIST), discount_percent=st.integers(min_value=0, max_value=100)
    ),
    st.builds(AccessTerms, kind=st.sampled_from([AccessKind.VIP, AccessKind.SUBSCRIPTION, AccessKind.BLACKLIST])),
)
holidays = st.frozensets(st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 12, 31)), max_size=5)


@settings(max_examples=300, deadline=None)
@given(
    rule_sets,
    entries,
    st.integers(min_value=1, max_value=20000),
    st.integers(min_value=0, max_value=5000),
    access_terms,
    holidays,
)
def test_deterministic_non_negative_monotone(
    r: TariffRules, entry: datetime, minutes: int, extra: int, access: AccessTerms, hol: frozenset[date]
) -> None:
    calendar = HolidayCalendar(hol)
    shorter = calculate(entry, entry + timedelta(minutes=minutes), r, access, calendar, TZ)
    again = calculate(entry, entry + timedelta(minutes=minutes), r, access, calendar, TZ)
    longer = calculate(entry, entry + timedelta(minutes=minutes + extra), r, access, calendar, TZ)
    assert shorter == again, "детерминизм"
    assert not shorter.amount.is_negative and not shorter.gross.is_negative and not shorter.discount.is_negative
    assert shorter.amount <= longer.amount, "монотонность по длительности при фиксированных правилах"
    assert shorter.amount + shorter.discount == shorter.gross
    assert shorter.billing_minutes <= longer.billing_minutes
