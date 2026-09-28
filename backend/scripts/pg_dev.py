"""Локальный PostgreSQL 16 для разработки без Docker и root (пакет pgserver).

Использование:
  python scripts/pg_dev.py start   -> печатает DSN, сервер живёт после выхода
  python scripts/pg_dev.py stop
  python scripts/pg_dev.py dsn
Данные — в backend/.pgdata (не коммитятся). Сокет — там же, TCP не слушает.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pgserver

PGDATA = Path(__file__).resolve().parent.parent / ".pgdata"
PGBIN = Path(pgserver.__file__).parent / "pginstall" / "bin"


def ensure_btree_gist() -> None:
    """После каждого `uv sync` pgserver переустанавливается и теряет contrib-модули — доставляем из .pgext/
    (btree_gist обязателен; pg_stat_statements/auto_explain/amcheck — если собраны, фаза 9)."""
    libdir = PGBIN.parent / "lib" / "postgresql"
    missing = [
        m
        for m in ("btree_gist", "pg_stat_statements", "auto_explain", "amcheck")
        if not (libdir / f"{m}.so").exists() and (PGDATA.parent / ".pgext" / m / f"{m}.so").exists()
    ]
    if missing:
        subprocess.run([str(PGDATA.parent / "scripts" / "build_pg_contrib.sh"), "install", *missing], check=True)


def dsn() -> str:
    return f"postgresql://postgres@/postgres?host={PGDATA}"


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dsn"
    if cmd == "start":
        ensure_btree_gist()
        srv = pgserver.get_server(str(PGDATA), cleanup_mode=None)
        print(srv.get_uri())
        return 0
    if cmd == "stop":
        subprocess.run([str(PGBIN / "pg_ctl"), "-D", str(PGDATA), "stop", "-m", "fast"], check=False)
        return 0
    if cmd == "dsn":
        print(dsn())
        return 0
    if cmd == "psql":
        os.execv(str(PGBIN / "psql"), [str(PGBIN / "psql"), dsn(), *sys.argv[2:]])  # noqa: S606 — намеренный exec psql
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
