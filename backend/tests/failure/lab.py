"""Лаборатория отказов (фаза 8 §5): отдельный кластер PostgreSQL 16 по TCP — primary (5541) + синхронная
standby (5542) — за toxiproxy (5540 → primary), Redis тестов (6390). Всё без root/Docker: бинарники pgserver,
toxiproxy-server из ~/.local/tools. Тесты пакета tests/failure управляют лабораторией напрямую:
toxics (разрыв сети), pg_ctl stop -m immediate (падение primary), pg_ctl promote (failover), redis stop/start.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pgserver

BACKEND = Path(__file__).resolve().parents[2]
PGBIN = Path(pgserver.__file__).parent / "pginstall" / "bin"
TOOLS = Path(os.environ.get("PARKING_TOOLS", Path.home() / ".local" / "tools"))
ROOT = BACKEND / ".failure-lab"


def _run(args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True, env=env)


@dataclass
class FailureLab:
    primary_port: int = 5541
    standby_port: int = 5542
    proxy_port: int = 5540
    toxiproxy_port: int = 8474
    redis_port: int = 6390
    _toxiproxy: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ paths / dsn
    @property
    def primary(self) -> Path:
        return ROOT / "primary"

    @property
    def standby(self) -> Path:
        return ROOT / "standby"

    @property
    def admin_dsn(self) -> str:
        """Админский DSN через toxiproxy — его получает conftest (PARKING_TEST_DSN) и всё приложение."""
        return f"postgresql://postgres@127.0.0.1:{self.proxy_port}/postgres"

    def direct_dsn(self, port: int, db: str = "postgres") -> str:
        return f"postgresql://postgres@127.0.0.1:{port}/{db}"

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def start(cls) -> FailureLab:
        lab = cls()
        lab.stop(quiet=True)
        shutil.rmtree(ROOT, ignore_errors=True)
        ROOT.mkdir(parents=True)
        lab._init_primary()
        lab._start_cluster(lab.primary, lab.primary_port)
        lab._init_standby()
        lab._start_cluster(lab.standby, lab.standby_port)
        lab._wait_sync_standby()
        lab._start_toxiproxy()
        lab.create_proxy()
        return lab

    def stop(self, *, quiet: bool = False) -> None:
        for data in (self.primary, self.standby):
            if (data / "postmaster.pid").exists():
                _run([str(PGBIN / "pg_ctl"), "-D", str(data), "stop", "-m", "immediate"], check=False)
        if self._toxiproxy is not None:
            self._toxiproxy.send_signal(signal.SIGTERM)
            try:
                self._toxiproxy.wait(5)
            except subprocess.TimeoutExpired:
                self._toxiproxy.kill()
            self._toxiproxy = None
        else:  # осиротевший toxiproxy от прошлого прогона
            _run(["pkill", "-f", f"toxiproxy-server -port {self.toxiproxy_port}"], check=False)
        if not quiet:
            shutil.rmtree(ROOT, ignore_errors=True)

    def _init_primary(self) -> None:
        _run(
            [
                str(PGBIN / "initdb"),
                "-D",
                str(self.primary),
                "-U",
                "postgres",
                "--auth=trust",
                "-E",
                "UTF8",
                "--locale=C",
            ]
        )
        conf = self.primary / "postgresql.auto.conf"
        conf.write_text(
            "\n".join(
                [
                    "listen_addresses = '127.0.0.1'",
                    f"port = {self.primary_port}",
                    f"unix_socket_directories = '{ROOT}'",
                    "wal_level = replica",
                    "max_wal_senders = 5",
                    "max_replication_slots = 5",
                    "synchronous_standby_names = 'standby1'",
                    "synchronous_commit = on",
                    "fsync = off",  # лаборатория: скорость важнее долговечности диска стенда
                    "log_min_messages = warning",
                    "",
                ]
            )
        )
        with (self.primary / "pg_hba.conf").open("a") as hba:
            hba.write("host replication postgres 127.0.0.1/32 trust\nhost all all 127.0.0.1/32 trust\n")

    def _init_standby(self) -> None:
        _run(
            [
                str(PGBIN / "pg_basebackup"),
                "-h",
                "127.0.0.1",
                "-p",
                str(self.primary_port),
                "-U",
                "postgres",
                "-D",
                str(self.standby),
                "-R",
                "-X",
                "stream",
                "-C",
                "-S",
                "standby1_slot",
            ]
        )
        (self.standby / "postgresql.auto.conf").write_text(
            "\n".join(
                [
                    "listen_addresses = '127.0.0.1'",
                    f"port = {self.standby_port}",
                    f"unix_socket_directories = '{ROOT}'",
                    "wal_level = replica",
                    "max_wal_senders = 5",
                    "synchronous_standby_names = ''",  # после promote новый primary не ждёт несуществующую реплику
                    "fsync = off",
                    "hot_standby = on",
                    "log_min_messages = warning",
                    f"primary_conninfo = 'host=127.0.0.1 port={self.primary_port} user=postgres application_name=standby1'",
                    "primary_slot_name = 'standby1_slot'",
                    "",
                ]
            )
        )

    def _start_cluster(self, data: Path, port: int) -> None:
        _run([str(PGBIN / "pg_ctl"), "-D", str(data), "-l", str(data / "server.log"), "-w", "-t", "60", "start"])
        self._wait_ready(port)

    def _wait_ready(self, port: int, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _run([str(PGBIN / "pg_isready"), "-h", "127.0.0.1", "-p", str(port)], check=False).returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError(f"postgres on {port} not ready")

    def _wait_sync_standby(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.psql(
                self.primary_port, "SELECT sync_state FROM pg_stat_replication WHERE application_name = 'standby1'"
            )
            if out.strip() == "sync":
                return
            time.sleep(0.3)
        raise RuntimeError("standby did not become synchronous")

    def psql(self, port: int, sql: str, db: str = "postgres") -> str:
        return _run(
            [str(PGBIN / "psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-d", db, "-tAc", sql]
        ).stdout

    # ------------------------------------------------------------------ toxiproxy
    def _start_toxiproxy(self) -> None:
        binary = TOOLS / "toxiproxy-server"
        if not binary.exists():
            raise RuntimeError(f"нет {binary}: см. docs/TESTING.md (toxiproxy)")
        self._toxiproxy = subprocess.Popen(
            [str(binary), "-port", str(self.toxiproxy_port), "-host", "127.0.0.1"],
            stdout=open(ROOT / "toxiproxy.log", "wb"),
            stderr=subprocess.STDOUT,
        )
        for _ in range(100):
            try:
                self._api("GET", "/version")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("toxiproxy did not start")

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.toxiproxy_port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def create_proxy(self, upstream_port: int | None = None) -> None:
        self._api("POST", "/reset")
        try:
            self._api("DELETE", "/proxies/pg")
        except Exception:
            pass
        self._api(
            "POST",
            "/proxies",
            {
                "name": "pg",
                "listen": f"127.0.0.1:{self.proxy_port}",
                "upstream": f"127.0.0.1:{upstream_port or self.primary_port}",
                "enabled": True,
            },
        )

    def repoint_proxy(self, upstream_port: int) -> None:
        """Failover: новый upstream — промотированная реплика (в проде это делает HAProxy/VIP по Patroni)."""
        self._api("POST", "/proxies/pg", {"upstream": f"127.0.0.1:{upstream_port}", "enabled": True})

    def add_toxic(self, name: str, toxic_type: str, attributes: dict[str, Any], stream: str = "downstream") -> None:
        self._api(
            "POST",
            "/proxies/pg/toxics",
            {"name": name, "type": toxic_type, "stream": stream, "toxicity": 1.0, "attributes": attributes},
        )

    def remove_toxic(self, name: str) -> None:
        self._api("DELETE", f"/proxies/pg/toxics/{name}")

    def partition(self, *, enabled: bool) -> None:
        """Полный разрыв API↔БД: timeout=0 на обоих направлениях держит соединения, не отвечая (не RST)."""
        if enabled:
            self.add_toxic("cut_down", "timeout", {"timeout": 0}, "downstream")
            self.add_toxic("cut_up", "timeout", {"timeout": 0}, "upstream")
        else:
            for name in ("cut_down", "cut_up"):
                try:
                    self.remove_toxic(name)
                except Exception:
                    pass

    # ------------------------------------------------------------------ outage / failover
    def stop_primary(self, mode: str = "fast") -> None:
        _run([str(PGBIN / "pg_ctl"), "-D", str(self.primary), "stop", "-m", mode, "-w", "-t", "60"], check=False)

    def start_primary(self) -> None:
        self._start_cluster(self.primary, self.primary_port)
        self._wait_sync_standby()

    def crash_primary(self) -> None:
        _run([str(PGBIN / "pg_ctl"), "-D", str(self.primary), "stop", "-m", "immediate"], check=False)

    def promote_standby(self) -> None:
        _run([str(PGBIN / "pg_ctl"), "-D", str(self.standby), "promote", "-w", "-t", "60"])
        self._wait_ready(self.standby_port)

    # ------------------------------------------------------------------ redis
    def redis(self, command: str) -> None:
        _run([sys.executable, str(BACKEND / "scripts" / "redis_dev.py"), command, str(self.redis_port)], check=False)
