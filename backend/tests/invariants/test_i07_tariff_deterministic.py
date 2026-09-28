"""I-7. Расчёт стоимости детерминирован: одни и те же (entry, exit, tariff_version, access_type, discount) всегда
дают один amount.

Механизм: tariff/domain/engine.calculate — чистая функция без I/O (домен не импортирует ни БД, ни HTTP, ни время
«сейчас»: now не читается, всё приходит аргументами); property-based тест на сотнях случайных правил.
При нарушении: два расчёта одной сессии дают разные суммы — спорные платежи, невозможная сверка.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from parking.modules.tariff.domain.engine import HolidayCalendar, calculate
from tests.tariff.test_engine import access_terms, entries, holidays, rule_sets

TZ = ZoneInfo("Asia/Tashkent")
DOMAIN = Path(__file__).resolve().parents[2] / "src" / "parking" / "modules" / "tariff" / "domain"
FORBIDDEN_IMPORTS = {"httpx", "sqlalchemy", "asyncpg", "redis", "requests", "aiohttp", "random", "os", "socket"}


def test_tariff_domain_has_no_io_and_no_clock() -> None:
    for path in DOMAIN.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
                assert not node.module.startswith("parking.modules.tariff.infrastructure"), path.name
            assert not (set(names) & FORBIDDEN_IMPORTS), f"{path.name}: I/O в домене тарифа: {names}"
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert name not in {"now", "utcnow", "today"}, f"{path.name}: чтение часов внутри расчёта"


@settings(max_examples=200, deadline=None)
@given(rule_sets, entries, st.integers(min_value=1, max_value=20000), access_terms, holidays)
def test_same_inputs_same_amount(
    r: object, entry: datetime, minutes: int, access: object, hol: frozenset[date]
) -> None:
    exit_at = entry + timedelta(minutes=minutes)
    first = calculate(entry, exit_at, r, access, HolidayCalendar(hol), TZ)  # type: ignore[arg-type]
    second = calculate(entry, exit_at, r, access, HolidayCalendar(hol), TZ)  # type: ignore[arg-type]
    third = calculate(entry, exit_at, r, access, HolidayCalendar(hol), TZ)  # type: ignore[arg-type]
    assert first == second == third
    assert first.to_snapshot() == second.to_snapshot(), "snapshot расчёта воспроизводим байт в байт"


def test_result_does_not_depend_on_wall_clock(monkeypatch: object) -> None:
    """Расчёт за фиксированные даты одинаков независимо от момента вызова — время вызова не участвует."""
    from parking.modules.tariff.domain.engine import AccessTerms
    from tests.tariff.test_engine import rules

    entry = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)
    terms = AccessTerms()
    a = calculate(entry, entry + timedelta(hours=3), rules(), terms, HolidayCalendar(frozenset()), TZ)
    b = calculate(entry, entry + timedelta(hours=3), rules(), terms, HolidayCalendar(frozenset()), TZ)
    assert a == b and a.gross.minor > 0
