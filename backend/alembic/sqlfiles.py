"""Исполнение SQL-файлов миграций по одному выражению (asyncpg не принимает несколько команд в prepared statement)."""

from __future__ import annotations

from pathlib import Path

from alembic import op

SQL_DIR = Path(__file__).resolve().parent / "sql"


def split_statements(sql: str) -> list[str]:
    """Режет SQL на выражения, уважая строки '...', комментарии -- и dollar-quoting $$ / $tag$."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    dollar_tag: str | None = None
    while i < n:
        ch = sql[i]
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        if ch == "'":
            end = i + 1
            while end < n:
                if sql[end] == "'" and not (end + 1 < n and sql[end + 1] == "'"):
                    break
                end += 2 if sql[end] == "'" else 1
            buf.append(sql[i : end + 1])
            i = end + 1
            continue
        if ch == "$":
            end = sql.find("$", i + 1)
            if end != -1 and (end == i + 1 or sql[i + 1 : end].replace("_", "a").isalnum()):
                dollar_tag = sql[i : end + 1]
                buf.append(dollar_tag)
                i = end + 1
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail and not all(line.strip().startswith("--") or not line.strip() for line in tail.splitlines()):
        statements.append(tail)
    return statements


def run_sql_file(name: str) -> None:
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    conn = op.get_bind()
    for stmt in split_statements(sql):
        conn.exec_driver_sql(stmt)
