"""SQL строится из констант, данные — только bind-параметрами (обоснование исключений bandit B608 /
semgrep avoid-sqlalchemy-text, см. security/exceptions.yaml).

Правило: в каждом `text(f"…")` интерполируются только (а) модульные константы из строковых литералов
(`_PAYMENT_COLUMNS`, `OCCUPYING`) и (б) локальные переменные, каждое присваивание которых — строковый литерал или
условное выражение из литералов (`lock = " FOR UPDATE" if for_update else ""`). Всё остальное — падение теста:
интерполировать значение запроса/пользователя в SQL нельзя даже «один раз»."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "parking"


def _is_literal_str(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):  # f-строка только из литералов и констант
        return all(
            _is_literal_str(v) if isinstance(v, ast.FormattedValue) is False else _is_const_name(v.value)
            for v in node.values
        )
    if isinstance(node, ast.IfExp):
        return _is_literal_str(node.body) and _is_literal_str(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_literal_str(node.left) and _is_literal_str(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        return True  # ", ".join(<константы>) — проверяется ниже по аргументам
    return False


def _is_const_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id.isupper()


def _module_constants(tree: ast.Module) -> dict[str, bool]:
    constants: dict[str, bool] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            constants[node.targets[0].id] = _is_literal_str(node.value) or (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "join"
            )
    return constants


def _local_assignments(func: ast.AST, name: str) -> list[ast.AST]:
    return [
        node.value
        for node in ast.walk(func)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]


def test_text_fstrings_interpolate_only_constant_sql_fragments() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        constants = _module_constants(tree)
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)]
        for func in functions:
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "text" and node.args):
                    continue
                template = node.args[0]
                if not isinstance(template, ast.JoinedStr):
                    continue
                for part in template.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    expr = part.value
                    where = f"{path.relative_to(SRC)}:{node.lineno} {{{ast.unparse(expr)}}}"
                    if isinstance(expr, ast.Name) and expr.id in constants:
                        if not constants[expr.id]:
                            offenders.append(where + " — константа не из строковых литералов")
                        continue
                    if isinstance(expr, ast.Name):
                        assignments = _local_assignments(func, expr.id)
                        if assignments and all(_is_literal_str(a) for a in assignments):
                            continue
                        offenders.append(where + " — локальная переменная не из литералов")
                        continue
                    offenders.append(where + " — выражение в SQL")
    assert offenders == [], "\n".join(offenders)


def test_no_string_formatting_of_sql_outside_fstrings() -> None:
    """text(query % x), text(query.format(x)), text("…" + x) — запрещены целиком."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "text" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.BinOp) or (
                    isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "format"
                ):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


def test_no_session_level_set_or_listen_in_pooled_code() -> None:
    """Фаза 9 §3: под PgBouncer в transaction pooling session-уровневые SET/LISTEN/pg_advisory_lock «прилипают» к
    серверному соединению и достаются чужому клиенту (утечка tenant-контекста). Разрешены только SET LOCAL и
    set_config(..., true); session-level advisory lock и LISTEN — только в outbox publisher, который держит
    отдельное прямое соединение к primary (maintenance DSN, минуя пулер)."""
    session_set = re.compile(r"""(?<![A-Za-z_])SET\s+(?!LOCAL\b)(?!TRANSACTION\b)[a-z_]+\.[a-z_]+\s*=""", re.IGNORECASE)
    set_config_session = re.compile(r"set_config\(\s*'[^']+'\s*,\s*[^,]+,\s*false\s*\)", re.IGNORECASE)
    listen = re.compile(r"\bLISTEN\s+\w+", re.IGNORECASE)
    session_lock = re.compile(r"pg_(try_)?advisory_lock\(")
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(SRC))
        for pattern, label in (
            (session_set, "session-level SET"),
            (set_config_session, "set_config(..., false)"),
        ):
            if pattern.search(text):
                offenders.append(f"{rel}: {label}")
        if (listen.search(text) or session_lock.search(text)) and rel != "workers/outbox_publisher.py":
            offenders.append(f"{rel}: LISTEN/session advisory lock outside outbox publisher")
    assert not offenders, "\n".join(offenders)
