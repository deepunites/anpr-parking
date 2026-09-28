"""I-13. Деньги — целые в минорных единицах (тийины) на всём пути: БД → домен → API → UI.

Механизм: тип Money без __float__ и с запретом float в арифметике; колонки *_amount/price/total — bigint (не numeric
с дробью, не float); Pydantic-схемы API не содержат float для денежных полей (AST + OpenAPI: денежные поля — integer
или строка форматирования); фронтенд получает amount целым и форматированную строку с бэкенда.
При нарушении: ошибки округления в платежах и сверке (33 % скидки, суммы по сменам), расхождения с провайдерами.
"""

from __future__ import annotations

import ast
import json
import re
from fractions import Fraction
from pathlib import Path

import asyncpg
import pytest

from parking.shared.money import Money, MoneyError, Rounding

ROOT = Path(__file__).resolve().parents[3]
API = ROOT / "backend" / "src" / "parking" / "api"
MONEY_NAME = re.compile(
    r"(amount|price|total|revenue|sum|fee|cost|discount|gross|net|balance|float$|cash)", re.IGNORECASE
)
NOT_MONEY = {"opening_float"}  # разменный фонд смены — тоже целое (проверяется ниже), имя совпадает с float


def test_money_has_no_float_conversion() -> None:
    money = Money(150000)
    assert not hasattr(money, "__float__")
    with pytest.raises(TypeError):
        float(money)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        money * 0.33  # type: ignore[operator]
    with pytest.raises(MoneyError):
        money.multiply(0.33, Rounding.HALF_UP)  # type: ignore[arg-type]
    assert money.multiply(Fraction(33, 100), Rounding.HALF_UP).minor == 49500
    assert isinstance(money.multiply(Fraction(1, 3), Rounding.DOWN).minor, int)


async def test_money_columns_are_integers(owner: asyncpg.Connection) -> None:
    rows = await owner.fetch(
        """
        SELECT c.relname AS t, a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND a.attnum > 0
          AND NOT a.attisdropped
          AND (a.attname ~* '(amount|price|total|revenue|_sum$|fee|cost|discount|gross|net_|balance|opening_float|closing)')
          AND a.attname NOT LIKE '%\\_at' AND a.attname NOT LIKE '%\\_percent' AND a.attname NOT LIKE '%\\_json'
        """
    )
    assert rows, "должны найтись денежные колонки"
    for r in rows:
        assert r["typ"] in {"bigint", "integer"}, f"{r['t']}.{r['col']}: {r['typ']} — деньги только целые"


def test_api_schemas_have_no_float_money_fields() -> None:
    offenders: list[str] = []
    for path in API.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if name in NOT_MONEY or not MONEY_NAME.search(name):
                    continue
                if "float" in ast.dump(node.annotation) or "Decimal" in ast.dump(node.annotation):
                    offenders.append(f"{path.relative_to(API)}:{node.lineno} {name}")
    assert offenders == [], offenders


def test_openapi_money_fields_are_integer_or_formatted_string() -> None:
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text())
    offenders: list[str] = []

    def walk(schema: dict[str, object], where: str) -> None:
        props = schema.get("properties")
        if isinstance(props, dict):
            for name, prop in props.items():
                if isinstance(prop, dict):
                    if MONEY_NAME.search(name) and name not in {"ratio"}:
                        types = {prop.get("type")} | {
                            t.get("type") for t in prop.get("anyOf", []) if isinstance(t, dict)
                        }  # type: ignore[union-attr]
                        if "number" in types:
                            offenders.append(f"{where}.{name}")
                    walk(prop, f"{where}.{name}")
        for key in ("items", "additionalProperties"):
            sub = schema.get(key)
            if isinstance(sub, dict):
                walk(sub, where)

    for name, schema in spec["components"]["schemas"].items():
        walk(schema, name)
    assert offenders == [], offenders
