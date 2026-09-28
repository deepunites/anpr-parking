"""Проверка миграций перед merge (фаза 9 §5): каждая ещё не применённая ревизия выполняется отдельно на копии схемы,
измеряется время и наблюдаются блокировки, которые держит транзакция alembic (опрос pg_locks каждые 50 мс).
Миграция, берущая ACCESS EXCLUSIVE на «большую» таблицу (по reltuples ≥ --big-rows или из списка --big), блокируется:
на production такая миграция остановит приём событий на время удержания блокировки.

    uv run python scripts/migration_check.py --dsn postgresql://postgres@/parking_dev?host=... [--schema-dump prod-schema.sql]
                                              [--big-rows 100000] [--big outbox,anpr_event,parking_session,payment]
                                              [--max-seconds 60] [--json report.json]

В CI (job migration-check): копия схемы staging (pg_dump --schema-only, ночной артефакт) + сид объёмов через
reltuples (ANALYZE после COPY выборки) — см. .github/workflows/ci.yml.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

BACKEND = Path(__file__).resolve().parents[1]
ALWAYS_BIG = {"outbox", "anpr_event", "anpr_idempotency", "parking_session", "payment", "audit_log", "consumer_inbox"}


def _with_db(dsn: str, db: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


SAFE_ALTER = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?P<table>[\w.\"]+)\s+(?P<action>(?:SET|RESET)\s*\(|ADD\s+CONSTRAINT[^;]*NOT\s+VALID|VALIDATE\s+CONSTRAINT|ATTACH\s+PARTITION|DETACH\s+PARTITION\s+\S+\s+CONCURRENTLY|ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+\S+(?:\s+DEFAULT\s+(?:'[^']*'|\d+|true|false|now\(\)|null))?\s*(?:,|;|$))",
    re.IGNORECASE,
)
ANY_ALTER = re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?P<table>[\w.\"]+)", re.IGNORECASE)
ALLOW_MARK = "-- migration-check: allow-lock"


def static_lock_review(revisions: list[str], big: set[str]) -> list[str]:
    """Статический разбор SQL ревизий: ALTER TABLE на большой таблице, не входящий в безопасный список (метаданные,
    NOT VALID + VALIDATE, партиции, ADD COLUMN с константным DEFAULT), требует явной пометки `-- migration-check:
    allow-lock` с обоснованием окна. Опрос pg_locks мог бы пропустить блокировку короче 50 мс — статика не пропустит."""
    findings: list[str] = []
    for rev in revisions:
        for sql_file in (BACKEND / "alembic" / "sql").glob(f"{rev}_up.sql"):
            text = sql_file.read_text(encoding="utf-8")
            if ALLOW_MARK in text:
                continue
            for statement in re.split(r";\s*\n", text):
                m = ANY_ALTER.search(statement)
                if not m:
                    continue
                table = m.group("table").strip('"').split(".")[-1]
                if table in big and not SAFE_ALTER.search(statement + ";"):
                    findings.append(
                        f"{rev}: ALTER TABLE {table} требует ACCESS EXCLUSIVE — {statement.strip()[:100]!r}"
                    )
    return findings


def pending_revisions(dsn: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "history", "-r", "current:head", "--indicate-current"],
        cwd=BACKEND,
        env={**os.environ, "PARKING_DATABASE_URL": dsn.replace("postgresql://", "postgresql+asyncpg://", 1)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    revs: list[str] = []
    for line in out.splitlines():  # формат: "<down> -> <rev> (head), message"
        if " -> " in line:
            rev = line.split(" -> ")[1].split()[0].rstrip(",")
            if "(current)" not in line:
                revs.append(rev)
    return list(reversed(revs))


async def observe_locks(dsn: str, dbname: str, stop: asyncio.Event) -> dict[str, set[str]]:
    """Собирает по каждому отношению множество режимов блокировок, которые держали бэкенды в scratch-БД."""
    seen: dict[str, set[str]] = {}
    conn = await asyncpg.connect(_with_db(dsn, "postgres"))
    try:
        while not stop.is_set():
            rows = await conn.fetch(
                """SELECT l.relation::regclass::text AS rel, l.mode
                   FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
                   WHERE l.locktype = 'relation' AND a.datname = $1 AND l.granted AND l.relation::regclass::text NOT LIKE 'pg_%'""",
                dbname,
            )
            for r in rows:
                seen.setdefault(r["rel"], set()).add(r["mode"])
            await asyncio.sleep(0.05)
    finally:
        await conn.close()
    return seen


async def table_sizes(dsn: str, dbname: str) -> dict[str, float]:
    conn = await asyncpg.connect(_with_db(dsn, dbname))
    try:
        rows = await conn.fetch(
            """SELECT c.oid::regclass::text AS rel, greatest(c.reltuples, 0) AS rows_est,
                      coalesce((SELECT sum(greatest(p.reltuples,0)) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhrelid WHERE i.inhparent = c.oid), 0) AS part_rows
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"""
        )
        return {r["rel"]: float(max(r["rows_est"], r["part_rows"])) for r in rows}
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True, help="admin DSN (postgres@…/postgres)")
    parser.add_argument("--scratch", default="parking_migration_check")
    parser.add_argument(
        "--schema-dump",
        type=Path,
        default=None,
        help="pg_dump --schema-only production/staging; иначе — alembic до current",
    )
    parser.add_argument("--big-rows", type=float, default=100_000)
    parser.add_argument("--big", default=",".join(sorted(ALWAYS_BIG)))
    parser.add_argument("--max-seconds", type=float, default=60.0)
    parser.add_argument("--json", type=Path, default=None)
    return parser.parse_args()


def prepare_scratch(args: argparse.Namespace, scratch: str, env: dict[str, str]) -> int:
    if args.schema_dump:
        asyncio.run(_apply_dump(scratch, args.schema_dump))
        return 0
    # копии схемы нет — применяем всё, кроме последних ревизий, чтобы проверить хотя бы их
    base = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head-1"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if base.returncode != 0:
        print(base.stderr[-1500:], file=sys.stderr)
    return base.returncode


async def _apply_dump(scratch: str, dump: Path) -> None:
    sql = await asyncio.to_thread(dump.read_text, encoding="utf-8")
    conn = await asyncpg.connect(scratch)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def main() -> int:
    args = parse_args()
    big = set(filter(None, args.big.split(",")))

    admin = await asyncpg.connect(args.dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{args.scratch}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{args.scratch}"')
    finally:
        await admin.close()
    scratch = _with_db(args.dsn, args.scratch)
    env = {**os.environ, "PARKING_DATABASE_URL": scratch.replace("postgresql://", "postgresql+asyncpg://", 1)}
    if await asyncio.to_thread(prepare_scratch, args, scratch, env):
        return 2

    report: list[dict[str, object]] = []
    failed = False
    revisions = pending_revisions(scratch)
    for finding in static_lock_review(revisions, big):
        print(f"blocked (static): {finding}")
        failed = True
    for rev in revisions:
        sizes = await table_sizes(args.dsn, args.scratch)
        stop = asyncio.Event()
        observer = asyncio.create_task(observe_locks(args.dsn, args.scratch, stop))
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            rev,
            cwd=BACKEND,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        elapsed = time.perf_counter() - t0
        stop.set()
        locks = await observer
        dangerous = sorted(
            rel
            for rel, modes in locks.items()
            if "AccessExclusiveLock" in modes and (rel.split(".")[-1] in big or sizes.get(rel, 0) >= args.big_rows)
        )
        verdict = "ok"
        if proc.returncode != 0:
            verdict = "error"
        elif dangerous:
            verdict = "blocked: ACCESS EXCLUSIVE on big tables"
        elif elapsed > args.max_seconds:
            verdict = f"blocked: {elapsed:.1f}s > {args.max_seconds}s"
        failed |= verdict != "ok"
        report.append(
            {
                "revision": rev,
                "seconds": round(elapsed, 2),
                "verdict": verdict,
                "access_exclusive": dangerous,
                "locks": {rel: sorted(m) for rel, m in locks.items() if "AccessExclusiveLock" in m or "ShareLock" in m},
                "stderr": err.decode()[-500:] if proc.returncode else "",
            }
        )
        print(f"{rev}: {elapsed:.2f}s {verdict}" + (f" {dangerous}" if dangerous else ""))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    admin = await asyncpg.connect(args.dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{args.scratch}" WITH (FORCE)')
    finally:
        await admin.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
