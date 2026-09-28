#!/usr/bin/env python
"""Лаборатория PostgreSQL HA фазы 9 на рабочей станции без root: Patroni 3.3 (DCS raft — в production etcd,
deploy/postgres/patroni.yml.j2), 3 узла (primary, sync standby, async standby с тегом nosync), PgBouncer 1.25 в
transaction-режиме, «HAProxy» (leader_proxy.py), WAL-архив и базовые бэкапы в MinIO (wal_archive.py).

    python pgha_lab.py start | stop | status
    python pgha_lab.py failover      # kill -9 primary → автоматический failover, RTO по записям через прокси
    python pgha_lab.py backup        # базовый бэкап (pg_basebackup -Ft) → MinIO
    python pgha_lab.py verify        # еженедельная проверка: восстановление + pg_amcheck + контрольные суммы → метрика
    python pgha_lab.py pitr          # учение: PITR на произвольную точку + замер RTO полного восстановления
    python pgha_lab.py pooler-test   # RLS под PgBouncer transaction pooling (pytest backend/tests/db/test_pgbouncer_rls.py)

Запуск из backend/: `uv run python ../deploy/postgres/lab/pgha_lab.py <cmd>` (нужны asyncpg, boto3, pgserver, uvx).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pgserver

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BACKEND = ROOT / "backend"
PGBIN = Path(pgserver.__file__).parent / "pginstall" / "bin"
LAB = BACKEND / ".pgha"
NODES = ["pg1", "pg2", "pg3"]
PG_PORTS = {"pg1": 5551, "pg2": 5552, "pg3": 5553}
REST_PORTS = {"pg1": 8011, "pg2": 8012, "pg3": 8013}
RAFT_PORTS = {"pg1": 2221, "pg2": 2222, "pg3": 2223}
PROXY_PORT = 5500  # «HAProxy» → primary
POOLER_PORT = 6432  # PgBouncer transaction pooling → PROXY_PORT
DB = os.environ.get("PGHA_DB", "parking_lab")  # PGHA_DB=parking_e2e — проверка на данных e2e-прогона
PYTHON = sys.executable
ENV_FILE = BACKEND / ".env.dev"


def s3_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    env.setdefault("PG_BACKUP_PREFIX", "lab")
    return env


def preload_libraries() -> str:
    """pg_stat_statements/auto_explain — только если собраны для pgserver (scripts/build_pg_contrib.sh); в production
    они в пакете PostgreSQL всегда (patroni.yml.j2)."""
    libdir = Path(
        subprocess.run(
            [str(PGBIN / "pg_config"), "--pkglibdir"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    return ",".join(lib for lib in ("pg_stat_statements", "auto_explain") if (libdir / f"{lib}.so").exists())


def patroni_config(name: str) -> dict[str, object]:
    node = LAB / name
    preload = preload_libraries()
    partners = [f"127.0.0.1:{RAFT_PORTS[n]}" for n in NODES if n != name]
    archive = f"{PYTHON} {HERE / 'wal_archive.py'}"
    return {
        "scope": "parking-lab",
        "name": name,
        "restapi": {"listen": f"127.0.0.1:{REST_PORTS[name]}", "connect_address": f"127.0.0.1:{REST_PORTS[name]}"},
        "raft": {
            "data_dir": str(node / "raft"),
            "self_addr": f"127.0.0.1:{RAFT_PORTS[name]}",
            "partner_addrs": partners,
        },
        "bootstrap": {
            "dcs": {
                "ttl": 20,  # production (etcd): ttl 30 / loop_wait 10 / retry_timeout 10 (patroni.yml.j2)
                "loop_wait": 5,
                "retry_timeout": 5,
                "maximum_lag_on_failover": 1_048_576,
                "synchronous_mode": True,  # RPO = 0 для критичных транзакций (app_critical: remote_apply)
                "synchronous_mode_strict": False,  # при потере sync standby primary продолжает писать (доступность > RPO для NORMAL)
                "postgresql": {
                    "use_pg_rewind": True,
                    "use_slots": True,
                    "parameters": {
                        # обоснования — docs/INFRA.md §3 (лаборатория: маленькие значения, prod — patroni.yml.j2)
                        "wal_level": "replica",
                        "hot_standby": "on",
                        "max_wal_senders": 10,
                        "max_replication_slots": 10,
                        "wal_log_hints": "on",
                        "wal_keep_size": "256MB",
                        "archive_mode": "on",
                        "archive_timeout": "60s",
                        "archive_command": f"{archive} push %p %f",
                        **(
                            {
                                "shared_preload_libraries": preload,
                                "pg_stat_statements.track": "all",
                                "auto_explain.log_min_duration": "500ms",
                                "auto_explain.log_analyze": "on",
                                "auto_explain.log_buffers": "on",
                                "auto_explain.log_format": "json",
                            }
                            if preload
                            else {}
                        ),
                        "log_min_duration_statement": "500ms",
                        "log_lock_waits": "on",
                        "log_checkpoints": "on",
                        "log_autovacuum_min_duration": "1s",
                        "shared_buffers": "128MB",
                        "effective_cache_size": "512MB",
                        "work_mem": "8MB",
                        "maintenance_work_mem": "64MB",
                        "checkpoint_timeout": "15min",
                        "checkpoint_completion_target": 0.9,
                        "max_wal_size": "512MB",
                        "min_wal_size": "128MB",
                        "autovacuum_vacuum_cost_limit": 800,
                        "autovacuum_naptime": "15s",
                        "idle_in_transaction_session_timeout": "60s",
                        "max_connections": 100,
                    },
                },
            },
            "initdb": [{"encoding": "UTF8"}, "data-checksums"],
            "pg_hba": [
                "local all all trust",
                "host all all 127.0.0.1/32 trust",
                "host replication replicator 127.0.0.1/32 trust",
            ],
        },
        "postgresql": {
            "listen": f"127.0.0.1:{PG_PORTS[name]}",
            "connect_address": f"127.0.0.1:{PG_PORTS[name]}",
            "data_dir": str(node / "data"),
            "bin_dir": str(PGBIN),
            "authentication": {
                "replication": {"username": "replicator", "password": "rep"},
                "superuser": {"username": "postgres", "password": "postgres"},
                "rewind": {"username": "rewind_user", "password": "rewind"},
            },
            "parameters": {"unix_socket_directories": str(node / "run")},
            "create_replica_methods": ["basebackup"],
        },
        "tags": {"nofailover": False, "noloadbalance": False, "clonefrom": False, "nosync": name == "pg3"},
    }


def _spawn(name: str, args: list[str], env: dict[str, str] | None = None) -> int:
    LAB.mkdir(exist_ok=True)
    log = open(LAB / f"{name}.log", "ab")  # noqa: SIM115
    proc = subprocess.Popen(args, cwd=BACKEND, stdout=log, stderr=log, env=env or s3_env(), start_new_session=True)
    (LAB / f"{name}.pid").write_text(str(proc.pid))
    return proc.pid


def _pid(name: str) -> int | None:
    p = LAB / f"{name}.pid"
    if not p.exists():
        return None
    try:
        pid = int(p.read_text())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError):
        return None


def rest(name: str, path: str = "") -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{REST_PORTS[name]}/{path}", timeout=1) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, json.loads(exc.read() or b"{}")
    except Exception:
        return 0, {}


def leader() -> str | None:
    for n in NODES:
        code, _ = rest(n, "primary")
        if code == 200:
            return n
    return None


def cluster() -> list[dict[str, object]]:
    for n in NODES:
        code, body = rest(n, "cluster")
        if code == 200:
            return list(body.get("members", []))  # type: ignore[union-attr]
    return []


def dsn(port: int, db: str = DB, user: str = "postgres") -> str:
    return f"postgresql://{user}@127.0.0.1:{port}/{db}"


def start_patroni(name: str) -> None:
    (LAB / name).mkdir(parents=True, exist_ok=True)
    (LAB / name / "run").mkdir(exist_ok=True)
    cfg = LAB / name / "patroni.yml"
    cfg.write_text(json.dumps(patroni_config(name), indent=2))  # YAML ⊃ JSON
    _spawn(name, ["uvx", "--from", "patroni[raft]==3.3.7", "--with", "psycopg2-binary", "patroni", str(cfg)])


def wait_leader(timeout: float = 120) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if (lead := leader()) is not None:
            return lead
        time.sleep(1)
    raise SystemExit("no leader")


def wait_members(count: int, timeout: float = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        members = cluster()
        if len([m for m in members if m.get("state") in ("running", "streaming")]) >= count:
            return
        time.sleep(2)
    raise SystemExit(f"cluster did not reach {count} running members: {cluster()}")


def start_pgbouncer() -> None:
    pb = LAB / "pgbouncer"
    pb.mkdir(exist_ok=True)
    (pb / "userlist.txt").write_text('"postgres" ""\n"app_critical" ""\n"app_bulk" ""\n"app_maintenance" ""\n')
    (pb / "pgbouncer.ini").write_text(
        f"""[databases]
{DB} = host=127.0.0.1 port={PROXY_PORT} dbname={DB}
* = host=127.0.0.1 port={PROXY_PORT}

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {POOLER_PORT}
unix_socket_dir = {pb}
auth_type = trust
auth_file = {pb / "userlist.txt"}
pool_mode = transaction
max_client_conn = 500
default_pool_size = 20
reserve_pool_size = 5
min_pool_size = 2
server_reset_query =
server_reset_query_always = 0
ignore_startup_parameters = extra_float_digits,options
max_prepared_statements = 200
server_lifetime = 1800
server_idle_timeout = 300
query_wait_timeout = 30
server_connect_timeout = 3
server_login_retry = 1
tcp_keepalive = 1
log_connections = 0
log_disconnections = 0
logfile = {pb / "pgbouncer.log"}
pidfile = {pb / "pgbouncer.pid"}
admin_users = postgres
"""
    )
    tools = Path.home() / ".local" / "tools"
    binary = os.environ.get("PGBOUNCER_BIN", str(tools / "pgbouncer" / "pgbouncer"))
    env = s3_env()
    env["LD_LIBRARY_PATH"] = os.environ.get("PGBOUNCER_LIBS", str(tools / "pgbouncer" / "lib"))
    _spawn("pgbouncer", [binary, str(pb / "pgbouncer.ini")], env=env)


async def _sql(port: int, sql: str, db: str = DB, user: str = "postgres") -> object:
    conn = await asyncpg.connect(dsn(port, db, user), timeout=5)
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


def sql(port: int, q: str, db: str = DB, user: str = "postgres") -> object:
    return asyncio.run(_sql(port, q, db, user))


def bootstrap_schema() -> None:
    lead = wait_leader()
    port = PG_PORTS[lead]
    asyncio.run(_create_db(port))
    url = dsn(port).replace("postgresql://", "postgresql+asyncpg://", 1)
    r = subprocess.run(
        [PYTHON, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "PARKING_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise SystemExit(r.stderr[-1500:])
    asyncio.run(_seed(port))


async def _create_db(port: int) -> None:
    conn = await asyncpg.connect(dsn(port, "postgres"))
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB):
            await conn.execute(f"CREATE DATABASE {DB}")
    finally:
        await conn.close()


async def _seed(port: int) -> None:
    conn = await asyncpg.connect(dsn(port))
    try:
        await conn.execute((BACKEND / "scripts" / "seed_dev.sql").read_text(encoding="utf-8"))
        if "pg_stat_statements" in preload_libraries():
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        libdir = Path(
            subprocess.run(
                [str(PGBIN / "pg_config"), "--pkglibdir"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        if (libdir / "amcheck.so").exists():
            await conn.execute("CREATE EXTENSION IF NOT EXISTS amcheck")  # pg_amcheck при проверке бэкапа (§4)
    finally:
        await conn.close()


def start() -> None:
    stop()
    shutil.rmtree(LAB, ignore_errors=True)
    LAB.mkdir()
    subprocess.run([PYTHON, str(BACKEND / "scripts" / "minio_dev.py"), "setup"], check=False, capture_output=True)
    subprocess.run(
        [PYTHON, str(HERE / "wal_archive.py"), "ls", ""], env=s3_env(), capture_output=True, check=False
    )  # прогрев/проверка доступа к MinIO
    ensure_bucket()
    subprocess.run([PYTHON, str(HERE / "wal_archive.py"), "purge"], env=s3_env(), check=True)  # архив прошлого кластера
    for n in NODES:
        start_patroni(n)
        time.sleep(2)
    lead = wait_leader()
    print(f"leader: {lead}")
    wait_members(3)
    _spawn(
        "proxy",
        [
            PYTHON,
            str(HERE / "leader_proxy.py"),
            str(PROXY_PORT),
            ",".join(f"127.0.0.1:{REST_PORTS[n]}" for n in NODES),
            ",".join(f"127.0.0.1:{PG_PORTS[n]}" for n in NODES),
        ],
    )
    time.sleep(1.5)
    bootstrap_schema()
    start_pgbouncer()
    time.sleep(1)
    print(status())


def ensure_bucket() -> None:
    import boto3
    from botocore.config import Config

    env = s3_env()
    s3 = boto3.client(
        "s3",
        endpoint_url=env.get("PG_BACKUP_ENDPOINT", "http://127.0.0.1:9000"),
        aws_access_key_id=env["PARKING_S3_ACCESS_KEY"],
        aws_secret_access_key=env["PARKING_S3_SECRET_KEY"],
        config=Config(s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )
    try:
        s3.head_bucket(Bucket="pg-backups")
    except Exception:
        s3.create_bucket(Bucket="pg-backups")
    s3.put_bucket_versioning(Bucket="pg-backups", VersioningConfiguration={"Status": "Enabled"})


def stop() -> None:
    for name in ["pgbouncer", "proxy", *reversed(NODES), "verify", "pitr"]:
        pid = _pid(name)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    time.sleep(2)
    for name in [*NODES, "verify", "pitr"]:
        data = LAB / name / "data"
        if data.exists():
            subprocess.run(
                [str(PGBIN / "pg_ctl"), "-D", str(data), "stop", "-m", "immediate"], check=False, capture_output=True
            )
    for p in LAB.glob("*.pid") if LAB.exists() else []:
        p.unlink(missing_ok=True)


def status() -> str:
    members = cluster()
    lines = [
        f"{m['name']:4} {m.get('role', '?'):14} {m.get('state', '?'):10} lag={m.get('lag', '-')} tl={m.get('timeline', '-')}"
        for m in members
    ]
    lines.append(f"proxy: {'up' if _pid('proxy') else 'down'}, pgbouncer: {'up' if _pid('pgbouncer') else 'down'}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- failover
async def _write_loop(stop_at: float, out: dict[str, object]) -> None:
    """Пишет через прокси (как приложение через PgBouncer→HAProxy) и фиксирует окно недоступности записи."""
    ok, fail, last_ok, first_fail, first_ok_after = 0, 0, None, None, None
    while time.time() < stop_at:
        t = time.time()
        try:
            conn = await asyncpg.connect(dsn(PROXY_PORT), timeout=2)
            try:
                await conn.execute("INSERT INTO lab_writes(at) VALUES (now())")
            finally:
                await conn.close()
            ok += 1
            last_ok = t
            if first_fail is not None and first_ok_after is None:
                first_ok_after = t
        except Exception:
            fail += 1
            if first_fail is None:
                first_fail = t
        await asyncio.sleep(0.2)
    out.update({"ok": ok, "fail": fail, "first_fail": first_fail, "first_ok_after": first_ok_after, "last_ok": last_ok})


def failover() -> None:
    lead = wait_leader()
    sql(PG_PORTS[lead], "CREATE TABLE IF NOT EXISTS lab_writes(id bigserial PRIMARY KEY, at timestamptz NOT NULL)")
    before = sql(PG_PORTS[lead], "SELECT count(*) FROM lab_writes")
    postmaster = int((LAB / lead / "data" / "postmaster.pid").read_text().splitlines()[0])
    result: dict[str, object] = {}
    loop = asyncio.new_event_loop()
    writer = loop.create_task(_write_loop(time.time() + 90, result))

    async def kill_after() -> None:
        await asyncio.sleep(5)
        print(f"kill -9 postmaster of {lead} (pid {postmaster}) at {datetime.now(tz=UTC):%H:%M:%S}")
        os.kill(
            postmaster, signal.SIGKILL
        )  # Patroni-агент жив, СУБД мертва: агент не сможет перезапустить? — сможет; поэтому убиваем и агент
        patroni_pid = _pid(lead)
        if patroni_pid:
            os.killpg(patroni_pid, signal.SIGKILL)
        result["killed_at"] = time.time()

    loop.run_until_complete(asyncio.gather(writer, kill_after()))
    new_leader = leader()
    killed = float(result["killed_at"])  # type: ignore[arg-type]
    rto = (float(result["first_ok_after"]) - killed) if result.get("first_ok_after") else None  # type: ignore[arg-type]
    after = sql(PROXY_PORT, "SELECT count(*) FROM lab_writes")
    print(
        f"GATE patroni failover: old primary {lead} killed → new primary {new_leader}; "
        f"writes ok={result['ok']} failed={result['fail']}; RTO (kill → first successful write via proxy) = "
        f"{rto:.1f}s; rows before {before}, after {after} (ok-count consistent: {int(after) - int(before) == int(result['ok'])})"  # type: ignore[call-overload]
    )
    print(status())
    print(f"restarting {lead} as replica…")
    start_patroni(lead)
    wait_members(3)
    print(status())


# ----------------------------------------------------------------------------- backup / verify / PITR
def backup() -> str:
    """Базовый бэкап с реплики (pg3, как в production — off-load), tar в MinIO; WAL архивируется непрерывно."""
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out = LAB / "backups" / stamp
    out.mkdir(parents=True)
    src = (
        PG_PORTS["pg3"]
        if any(m["name"] == "pg3" and m.get("state") in ("running", "streaming") for m in cluster())
        else PG_PORTS[wait_leader()]
    )
    t0 = time.time()
    subprocess.run(
        [
            str(PGBIN / "pg_basebackup"),
            "-h",
            "127.0.0.1",
            "-p",
            str(src),
            "-U",
            "postgres",
            "-D",
            str(out),
            "-Ft",
            "-z",
            "-X",
            "none",
            "--checkpoint=fast",
            "--label",
            stamp,
        ],
        check=True,
        capture_output=True,
    )
    for f in out.iterdir():
        subprocess.run(
            [PYTHON, str(HERE / "wal_archive.py"), "put-file", str(f), f"base/{stamp}/{f.name}"],
            env=s3_env(),
            check=True,
        )
    print(
        f"GATE backup {stamp}: {sum(f.stat().st_size for f in out.iterdir()) / 1e6:.1f} MB → MinIO pg-backups/lab/base/{stamp}/ in {time.time() - t0:.1f}s"
    )
    return stamp


def _latest_backup() -> str:
    env = s3_env()
    r = subprocess.run(
        [PYTHON, str(HERE / "wal_archive.py"), "ls", "base/"], env=env, capture_output=True, text=True, check=True
    )
    stamps = sorted({line.split("/")[2] for line in r.stdout.split() if line.startswith("lab/base/")})
    if not stamps:
        raise SystemExit("no base backups in MinIO")
    return stamps[-1]


def restore(stamp: str, name: str, port: int, target_time: str | None) -> float:
    """Восстановление в изолированный инстанс (порт port): base.tar.gz + WAL из MinIO, PITR до target_time."""
    inst = LAB / name
    if (inst / "data").exists():  # остатки прошлого прогона: остановить, иначе порт занят
        subprocess.run(
            [str(PGBIN / "pg_ctl"), "-D", str(inst / "data"), "stop", "-m", "immediate"],
            check=False,
            capture_output=True,
        )
    shutil.rmtree(inst, ignore_errors=True)
    (inst / "data").mkdir(parents=True)
    (inst / "run").mkdir()
    env = s3_env()
    t0 = time.time()
    for member in ("base.tar.gz",):
        subprocess.run(
            [PYTHON, str(HERE / "wal_archive.py"), "get-file", f"base/{stamp}/{member}", str(inst / member)],
            env=env,
            check=True,
        )
        with tarfile.open(inst / member, "r:gz") as tar:
            tar.extractall(inst / "data", filter="data")
    data = inst / "data"
    # бэкап снят с реплики: в каталоге standby.signal и primary_conninfo Patroni — восстанавливаемый инстанс
    # должен быть самостоятельным (archive recovery → promote), а не подключаться к primary как ещё одна реплика
    (data / "standby.signal").unlink(missing_ok=True)
    with (data / "postgresql.auto.conf").open("a") as conf:
        conf.write(
            f"\nport = {port}\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = '{inst / 'run'}'\n"
            f"primary_conninfo = ''\nprimary_slot_name = ''\n"
            f"restore_command = '{PYTHON} {HERE / 'wal_archive.py'} get %f %p'\narchive_mode = off\n"
            f"recovery_target_action = 'promote'\n"
            + (f"recovery_target_time = '{target_time}'\n" if target_time else "")
        )
    (data / "recovery.signal").touch()
    for f in data.glob("*.pid"):
        f.unlink()
    os.chmod(data, 0o700)
    subprocess.run(
        [str(PGBIN / "pg_ctl"), "-D", str(data), "-l", str(inst / "postgres.log"), "-w", "-t", "300", "start"],
        check=True,
        capture_output=True,
        env=env,  # restore_command читает креды MinIO из окружения
    )
    for _ in range(300):
        try:
            if not sql(port, "SELECT pg_is_in_recovery()", db="postgres"):
                break
        except Exception:
            pass
        time.sleep(1)
    return time.time() - t0


def switch_and_wait_archived(port: int, timeout: float = 90) -> str:
    """pg_switch_wal + ожидание, пока архиватор доставит закрытый сегмент в MinIO (иначе бэкап невосстановим
    до «сейчас»: checkpoint record ещё в незаархивированном сегменте)."""
    name = sql(port, "SELECT pg_walfile_name(pg_switch_wal())", db="postgres")
    t0 = time.time()
    while time.time() - t0 < timeout:
        last = sql(port, "SELECT last_archived_wal FROM pg_stat_archiver", db="postgres")
        if last is not None and str(last) >= str(name):
            return str(name)
        time.sleep(1)
    raise SystemExit(f"segment {name} was not archived within {timeout}s (last={last})")


def checksums(port: int) -> dict[str, object]:
    day = datetime.now(tz=UTC).date().isoformat()
    return {
        "sessions_today": sql(port, f"SELECT count(*) FROM parking_session WHERE created_at::date = '{day}'"),
        "payments_sum_today": sql(
            port, f"SELECT coalesce(sum(amount),0) FROM payment WHERE status='PAID' AND paid_at::date = '{day}'"
        ),
        "sessions_total": sql(port, "SELECT count(*) FROM parking_session"),
        "payments_total": sql(port, "SELECT count(*) FROM payment"),
        "outbox_total": sql(port, "SELECT count(*) FROM outbox"),
    }


def verify() -> None:
    """Еженедельная автоматическая проверка бэкапа (§4): восстановление в изолированный инстанс, pg_amcheck,
    контрольные суммы бизнес-данных совпадают с источником → метрика backup_restore_verified_timestamp."""
    stamp = backup()
    lead = wait_leader()
    expected = checksums(PG_PORTS[lead])
    switch_and_wait_archived(PG_PORTS[lead])  # бэкап восстановим до «сейчас»
    port = 5560
    elapsed = restore(stamp, "verify", port, None)
    amcheck = subprocess.run(
        [
            str(PGBIN / "pg_amcheck"),
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "postgres",
            "-d",
            DB,
            "--install-missing",
            "--heapallindexed",
            "--parent-check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    actual = checksums(port)
    subprocess.run(
        [str(PGBIN / "pg_ctl"), "-D", str(LAB / "verify" / "data"), "stop", "-m", "fast"],
        check=False,
        capture_output=True,
    )
    ok = amcheck.returncode == 0 and actual == expected
    metric_dir = LAB / "textfile"
    metric_dir.mkdir(exist_ok=True)
    now = int(time.time())
    (metric_dir / "backup_restore_verified.prom").write_text(
        f"# HELP backup_restore_verified_timestamp Unix time последней успешной проверки восстановления бэкапа\n"
        f'# TYPE backup_restore_verified_timestamp gauge\nbackup_restore_verified_timestamp{{cluster="parking-lab"}} {now if ok else 0}\n'
    )
    print(
        f"GATE backup verify: backup {stamp} restored in {elapsed:.1f}s; pg_amcheck rc={amcheck.returncode}; "
        f"checksums source={expected} restored={actual} match={actual == expected} → backup_restore_verified_timestamp="
        f"{now if ok else 0} ({metric_dir / 'backup_restore_verified.prom'})"
    )
    if not ok:
        print(amcheck.stdout[-2000:], amcheck.stderr[-2000:])
        raise SystemExit(1)


def pitr() -> None:
    """Учение: полная потеря primary → восстановление из бэкапа в произвольную точку времени с замером RTO."""
    lead = wait_leader()
    port = PG_PORTS[lead]
    sql(
        port, "CREATE TABLE IF NOT EXISTS pitr_marks(id bigserial PRIMARY KEY, note text, at timestamptz DEFAULT now())"
    )
    stamp = backup()
    for i in range(3):
        sql(port, f"INSERT INTO pitr_marks(note) VALUES ('before-target-{i}')")
    time.sleep(1.5)
    target = sql(port, "SELECT now()")
    time.sleep(1.5)
    for i in range(3):
        sql(port, f"INSERT INTO pitr_marks(note) VALUES ('after-target-{i}')")
    switch_and_wait_archived(port)
    before_target = sql(port, f"SELECT count(*) FROM pitr_marks WHERE at <= '{target}'")
    total = sql(port, "SELECT count(*) FROM pitr_marks")
    t0 = time.time()
    elapsed = restore(stamp, "pitr", 5561, str(target))
    restored = sql(5561, "SELECT count(*) FROM pitr_marks")
    after = sql(5561, "SELECT count(*) FROM pitr_marks WHERE note LIKE 'after-target-%'")
    subprocess.run(
        [str(PGBIN / "pg_ctl"), "-D", str(LAB / "pitr" / "data"), "stop", "-m", "fast"],
        check=False,
        capture_output=True,
    )
    print(
        f"GATE PITR: target={target}; source rows total={total}, ≤target={before_target}; restored instance rows={restored}, "
        f"after-target rows={after} (must be 0); restore RTO={elapsed:.1f}s (wall {time.time() - t0:.1f}s), SLO 15 min"
    )
    if restored != before_target or int(after) != 0:  # type: ignore[call-overload]
        raise SystemExit("PITR mismatch")


def pooler_test() -> None:
    env = {**os.environ, "PARKING_POOLER_DSN": dsn(POOLER_PORT), "PARKING_POOLER_DIRECT_DSN": dsn(PROXY_PORT)}
    r = subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-s",
            "tests/db/test_pgbouncer_rls.py",
            "-m",
            "pooler",
            "-o",
            "addopts=",
        ],
        cwd=BACKEND,
        env=env,
        check=False,
    )
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {
        "start": start,
        "stop": stop,
        "status": lambda: print(status()),
        "failover": failover,
        "backup": backup,
        "verify": verify,
        "pitr": pitr,
        "pooler-test": pooler_test,
    }[cmd]()
