"""v2 этап 7: правила дашбордов без БД — период в часовом поясе клиента, доступность камер, сигналы, MRR."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from parking.modules.reports.domain.dashboard import (
    Period,
    Severity,
    attention_reasons,
    availability,
    cash_signal,
    limit_signals,
    manual_open_signal,
    mrr,
)
from parking.shared.errors import ValidationError

T0 = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


def test_period_defaults_to_last_week_in_client_timezone() -> None:
    p = Period.parse(None, None, "Asia/Tashkent", date(2026, 9, 28))
    assert (p.start, p.end, p.days) == (date(2026, 9, 22), date(2026, 9, 28), 7)
    a, b = p.bounds()
    assert a == datetime(2026, 9, 21, 19, 0, tzinfo=UTC)  # полночь Ташкента (+05)
    assert b - a == timedelta(days=7)
    prev = p.previous()
    assert (prev.start, prev.end) == (date(2026, 9, 15), date(2026, 9, 21))
    assert len(p.dates()) == 7


@pytest.mark.parametrize(
    ("start", "end"), [(date(2026, 9, 10), date(2026, 9, 1)), (date(2026, 1, 1), date(2026, 9, 1))]
)
def test_period_rejects_reversed_and_too_long(start: date, end: date) -> None:
    with pytest.raises(ValidationError):
        Period.parse(start, end, "UTC", date(2026, 9, 28))


def test_availability_counts_only_known_time() -> None:
    end = T0 + timedelta(hours=10)
    # 4 ч онлайн, 1 ч офлайн, 5 ч онлайн → 90 %
    points = [(T0 + timedelta(hours=4), "OFFLINE"), (T0 + timedelta(hours=5), "ONLINE")]
    assert availability("ONLINE", points, T0, end) == 0.9
    # до первого контакта (UNKNOWN) время не считается: 2 ч неизвестно, затем 8 ч онлайн → 100 %
    assert availability("UNKNOWN", [(T0 + timedelta(hours=2), "ONLINE")], T0, end) == 1.0
    assert availability(None, [], T0, end) is None
    # переход до начала периода задаёт стартовый статус; после конца — игнорируется
    assert (
        availability(None, [(T0 - timedelta(hours=1), "OFFLINE"), (end + timedelta(hours=1), "ONLINE")], T0, end) == 0.0
    )
    assert availability("DEGRADED", [], T0, end) == 1.0  # деградация — события ещё принимаются


def test_cash_signal_only_for_real_discrepancies() -> None:
    assert cash_signal([0, 0]) is None
    s = cash_signal([-50_000, 0, 20_000])
    assert s is not None and s.code == "CASH_DISCREPANCY" and s.severity is Severity.BAD
    assert s.params == {"shifts": 2, "shortage": 50_000, "surplus": 20_000}
    surplus_only = cash_signal([10_000])
    assert surplus_only is not None and surplus_only.severity is Severity.WARN


def test_manual_open_signal_needs_volume_and_share() -> None:
    assert manual_open_signal(4, 4, "Ali", 4) is None  # мало — шум
    assert manual_open_signal(6, 100, "Ali", 6) is None  # 6 % выездов — норма
    warn = manual_open_signal(12, 100, "Ali", 9)
    assert warn is not None and warn.severity is Severity.WARN and warn.params["operator"] == "Ali"
    bad = manual_open_signal(25, 100, "Ali", 20)
    assert bad is not None and bad.severity is Severity.BAD
    no_exits = manual_open_signal(5, 0, None, 0)
    assert no_exits is not None and no_exits.params["share"] == 1.0


def test_limits_mrr_and_attention() -> None:
    signals = limit_signals([("lots", 1, 1), ("places", 90, 100), ("cameras", 3, 8), ("lots", 5, None)])
    assert [(s.params["kind"], s.severity) for s in signals] == [("lots", Severity.BAD), ("places", Severity.WARN)]
    assert mrr([49_000_000, None, 149_000_000]) == (198_000_000, 1)
    now = T0
    assert attention_reasons(
        status="TRIAL",
        trial_ends_at=now + timedelta(days=2),
        paid_until=None,
        cameras_offline=1,
        sessions_7d=0,
        lots=1,
        now=now,
    ) == ["TRIAL_ENDING", "CAMERAS_OFFLINE", "NO_TRAFFIC"]
    assert attention_reasons(
        status="ACTIVE",
        trial_ends_at=None,
        paid_until=now - timedelta(days=1),
        cameras_offline=0,
        sessions_7d=10,
        lots=1,
        now=now,
    ) == ["PAYMENT_OVERDUE"]
    assert (
        attention_reasons(
            status="ACTIVE", trial_ends_at=None, paid_until=None, cameras_offline=0, sessions_7d=0, lots=0, now=now
        )
        == []
    )
