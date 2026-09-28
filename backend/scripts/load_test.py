"""Нагрузочный прогон k6 (фаза 8 §6) на реальном стенде с замерами и сверкой.

    uv run python scripts/load_test.py --scenario baseline --scale 30 [--workers 2] [--lab]

Поднимает стенд (backend/scripts/e2e_stack.py; с --lab — на primary+standby лаборатории отказов, чтобы измерить лаг
репликации), запускает k6 (load/k6/parking.js), во время прогона раз в 5 с снимает: outbox_pending_total,
гистограмму exit_end_to_end_seconds, соединения БД, лаг реплики, размер WAL, латентность Redis, RSS процессов;
после — сверка «отправлено == в БД + в DLQ», дубликаты сессий, критерии §6; отчёт — docs/backend/load/<scenario>.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
K6 = Path(os.environ.get("K6_BIN", Path.home() / ".local" / "tools" / "k6"))
STATE = ROOT / "frontend" / "e2e" / ".stack.json"
RUN_DIR = BACKEND / ".e2e"
REPORT_DIR = ROOT / "docs" / "backend" / "load"
CRITERIA = {
    "p95_ms": 150.0,
    "p99_ms": 400.0,
    "error_rate": 0.001,
    "exit_e2e_p95_s": 2.0,
    "duplicate_sessions": 0,
    "lost_events": 0,
    "rss_growth": 0.10,
    "replica_lag_s": 5.0,
}


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — 127.0.0.1
        return str(resp.read().decode())


WORKER_METRIC_FAMILIES = (  # метрики воркеров, которые складываем к API: SLI выезда наблюдается в consumer'е
    "exit_end_to_end_seconds",
    "redis_stream_lag",
    "consumer_processed_total",
    "consumer_retry_scheduled_total",
    "dlq_messages_total",
    "barrier_command_total",
    "media_pending_frames",
)


def _parse_metrics(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


def _metrics(worker_urls: dict[str, str]) -> dict[str, float]:
    out = _parse_metrics(_get("http://127.0.0.1:8010/metrics"))
    for url in worker_urls.values():
        try:
            worker = _parse_metrics(_get(url))
        except OSError:
            continue
        for name, value in worker.items():
            if name.startswith(WORKER_METRIC_FAMILIES):
                out[name] = out.get(name, 0.0) + value
    return out


def _buckets(metrics: dict[str, float], prefix: str, label: str) -> dict[float, float]:
    found: dict[float, float] = {}
    for key, value in metrics.items():
        if key.startswith(prefix + "_bucket") and label in key:
            m = re.search(r'le="([^"]+)"', key)
            if m:
                found[float("inf") if m.group(1) == "+Inf" else float(m.group(1))] = value
    return found


def _quantile(buckets: dict[float, float], q: float) -> float | None:
    """Оценка квантиля из кумулятивной гистограммы (как histogram_quantile в Prometheus)."""
    if not buckets or buckets[max(buckets)] == 0:
        return None
    total = buckets[max(buckets)]
    target = total * q
    prev_le, prev_count = 0.0, 0.0
    for le in sorted(buckets):
        count = buckets[le]
        if count >= target:
            if le == float("inf"):
                return prev_le
            if count == prev_count:
                return le
            return prev_le + (le - prev_le) * (target - prev_count) / (count - prev_count)
        prev_le, prev_count = le, count
    return None


def _psql(dsn: str, sql: str) -> str:
    import pgserver

    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    return subprocess.run([str(psql), dsn, "-tAc", sql], capture_output=True, text=True, check=False).stdout.strip()


def _rss_kb(pid: int) -> int:
    total = 0
    try:
        children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False).stdout.split()
    except OSError:
        children = []
    for p in [pid, *map(int, children)]:
        try:
            for line in (Path(f"/proc/{p}/status")).read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
        except OSError:
            continue
    return total


@dataclass
class Samples:
    outbox_pending: list[float] = field(default_factory=list)
    stream_lag: list[float] = field(default_factory=list)  # сумма pending по шардам (лаг очередей)
    db_connections: list[int] = field(default_factory=list)
    replica_lag: list[float] = field(default_factory=list)
    redis_latency_ms: list[float] = field(default_factory=list)
    rss_kb: dict[str, list[int]] = field(default_factory=dict)
    wal_bytes: list[int] = field(default_factory=list)
    first_metrics: dict[str, float] = field(default_factory=dict)
    last_metrics: dict[str, float] = field(default_factory=dict)


class Sampler(threading.Thread):
    def __init__(self, dsn: str, standby_dsn: str | None, pids: dict[str, int], worker_metrics: dict[str, str]) -> None:
        super().__init__(daemon=True)
        self.dsn, self.standby_dsn, self.pids = dsn, standby_dsn, pids
        self.worker_metrics = worker_metrics
        self.samples = Samples()
        self.stop = threading.Event()
        self._wal_start: int | None = None

    def run(self) -> None:
        import redis

        client = redis.Redis(host="127.0.0.1", port=6390, socket_timeout=2)
        while not self.stop.is_set():
            try:
                metrics = _metrics(self.worker_metrics)
                if not self.samples.first_metrics:
                    self.samples.first_metrics = metrics
                self.samples.last_metrics = metrics
                self.samples.outbox_pending.append(metrics.get("outbox_pending_total", 0.0))
                self.samples.stream_lag.append(sum(v for k, v in metrics.items() if k.startswith("redis_stream_lag{")))
                conns = _psql(self.dsn, "SELECT count(*) FROM pg_stat_activity WHERE datname = 'parking_e2e'")
                self.samples.db_connections.append(int(conns or 0))
                lsn = _psql(self.dsn, "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')")
                if lsn:
                    if self._wal_start is None:
                        self._wal_start = int(lsn)
                    self.samples.wal_bytes.append(int(lsn) - self._wal_start)
                if self.standby_dsn:
                    lag = _psql(
                        self.dsn, "SELECT COALESCE(EXTRACT(EPOCH FROM max(replay_lag)), 0) FROM pg_stat_replication"
                    )
                    self.samples.replica_lag.append(float(lag or 0))
                started = time.perf_counter()
                client.ping()
                self.samples.redis_latency_ms.append((time.perf_counter() - started) * 1000)
                for name, pid in self.pids.items():
                    self.samples.rss_kb.setdefault(name, []).append(_rss_kb(pid))
            except Exception as exc:
                print(f"sampler: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.stop.wait(5.0)


def _xlen(stream: str) -> int:
    import redis

    return int(redis.Redis(host="127.0.0.1", port=6390, socket_timeout=2).xlen(stream))


def _login(api: str, user: dict[str, str]) -> str:
    req = urllib.request.Request(  # noqa: S310
        f"{api}/api/v1/auth/login",
        data=json.dumps({"email": user["email"], "password": user["password"]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return str(json.loads(resp.read())["access_token"])


def _post(api: str, path: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310
        f"{api}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return dict(json.loads(resp.read()))


def _camera_keys(
    api: str, admin_token: str, lot_id: str, per_direction: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Профиль §3: ~6 камер на площадку, но нагрузка распределена по тысячам камер — один ключ на всех упёрся бы в
    лимит 120 событий/мин на ключ (429). Создаём per_direction камер IN/OUT и ключ на каждую."""
    keys: dict[str, list[dict[str, str]]] = {"IN": [], "OUT": []}
    for direction in ("IN", "OUT"):
        for i in range(per_direction):
            code = f"K6_{direction}_{i:03d}"
            camera = _post(
                api,
                "/api/v1/cameras",
                admin_token,
                {
                    "lot_id": lot_id,
                    "code": code,
                    "name": f"k6 {direction} {i}",
                    "direction": direction,
                    "reason": "load test",
                },
            )
            issued = _post(
                api, f"/api/v1/cameras/{camera['id']}/keys", admin_token, {"label": "k6", "reason": "load test"}
            )
            keys[direction].append({"key": str(issued["key"]), "code": code})
    return keys["IN"], keys["OUT"]


def _vip_plates(api: str, admin_token: str, lot_id: str, count: int) -> list[str]:
    """Доля машин с льготой (абонемент/VIP) выезжает без оплаты — по ним измеряется SLI EXIT end-to-end (paid="no")."""
    plates = [f"{10 + i // 26:02d}V{100 + i % 900:03d}IP" for i in range(count)]
    for pl in plates:
        _post(
            api,
            "/api/v1/access-rules",
            admin_token,
            {
                "plate": pl,
                "kind": "VIP",
                "lot_id": lot_id,
                "valid_from": "2020-01-01T00:00:00+00:00",
                "reason": "load test",
            },
        )
    return plates


def main() -> int:  # noqa: PLR0915 — линейный сценарий прогона
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="baseline", choices=["baseline", "peak", "absolute", "soak", "operators"])
    parser.add_argument("--scale", type=int, default=int(os.environ.get("SCALE", "30")), help="делитель длительностей")
    parser.add_argument(
        "--rate-scale",
        type=int,
        default=int(os.environ.get("RATE_SCALE", "1")),
        help="делитель интенсивностей (слабый стенд)",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--lab", action="store_true", help="стенд на primary+standby лаборатории (лаг репликации)")
    parser.add_argument("--keep", action="store_true", help="не останавливать стенд после прогона")
    parser.add_argument(
        "--cameras", type=int, default=50, help="камер на направление (ключей) для распределения лимита"
    )
    args = parser.parse_args()
    if not K6.exists():
        raise SystemExit(f"нет k6: {K6}")

    lab = None
    env = {**os.environ, "PARKING_E2E_WORKERS": str(args.workers)}
    standby_dsn = None
    if args.lab:
        sys.path.insert(0, str(BACKEND))
        from tests.failure.lab import FailureLab

        lab = FailureLab.start()
        env["PARKING_E2E_DSN"] = lab.direct_dsn(lab.primary_port)
        standby_dsn = lab.direct_dsn(lab.standby_port)
    subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "e2e_stack.py"), "start"], check=True, env=env, capture_output=True
    )
    state = json.loads(STATE.read_text())
    api = state["api"]
    db_url = state["db_url"].replace("app_maintenance@", "postgres@")
    pids = {
        name: int((RUN_DIR / f"{name}.pid").read_text())
        for name in ("api", "consumer", "publisher", "replayer")
        if (RUN_DIR / f"{name}.pid").exists()
    }
    run_id = f"k6-{args.scenario}-{int(time.time())}"
    token = _login(api, state["users"]["operator"])
    admin_token = _login(api, state["users"]["tenant_admin"])
    entry_keys, exit_keys = _camera_keys(api, admin_token, state["lot_id"], args.cameras)
    vip_plates = _vip_plates(api, admin_token, state["lot_id"], 200)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = ROOT / "load" / "results" / f"{run_id}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    sampler = Sampler(db_url, standby_dsn, pids, state.get("metrics", {}))
    sampler.start()
    started = time.time()
    k6_env = {
        **os.environ,
        "API": api,
        "ENTRY_KEY": state["camera_keys"]["entry"],
        "ENTRY_CAMERAS": json.dumps(entry_keys),
        "EXIT_CAMERAS": json.dumps(exit_keys),
        "VIP_PLATES": json.dumps(vip_plates),
        "EXIT_KEY": state["camera_keys"]["exit"],
        "LOT_ID": state["lot_id"],
        "TOKEN": token,
        "RUN_ID": run_id,
        "K6_SCENARIO": args.scenario,
        "SCALE": str(args.scale),
        "RATE_SCALE": str(args.rate_scale),
    }
    k6 = subprocess.run(
        [str(K6), "run", "--quiet", "--summary-export", str(summary_path), str(ROOT / "load" / "k6" / "parking.js")],
        env=k6_env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.time() - started
    time.sleep(6)  # хвост: consumer/replayer дорабатывают, последний снимок метрик
    sampler.stop.set()
    sampler.join(timeout=15)
    (REPORT_DIR / f"{args.scenario}-k6.log").write_text(k6.stdout + k6.stderr)

    summary = json.loads(summary_path.read_text())
    m = summary["metrics"]

    def metric(name: str, key: str) -> float:
        return float(m.get(name, {}).get(key, 0.0))

    accepted = int(metric("events_accepted", "count"))
    queued = int(metric("events_queued", "count"))
    duplicates = int(metric("events_duplicate", "count"))
    rejected = int(metric("events_rejected", "count"))
    failed = int(metric("events_failed", "count"))
    camera = m.get("http_req_duration{name:camera_events}", {})
    p95, p99 = float(camera.get("p(95)", 0)), float(camera.get("p(99)", 0))
    requests = int(metric("http_reqs", "count"))
    error_rate = float(m.get("http_req_failed{name:camera_events}", {}).get("value", 0.0))

    # --- сверка: принято сервером == в БД (по метке RUN_ID) + DLQ + остаток оффлайн-буфера
    for _ in range(24):  # replay оффлайн-буфера, если был
        offline_left = int(
            (
                _psql(db_url, "SELECT 0")
                and subprocess.run(
                    [sys.executable, "-c", "import redis;print(redis.Redis(port=6390).xlen('anpr:offline'))"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
            )
            or 0
        )
        if offline_left == 0:
            break
        time.sleep(5)
    in_db = int(
        _psql(db_url, f"SELECT count(*) FROM anpr_event WHERE recognition->>'version' = '{run_id}'") or 0  # noqa: S608 — run_id генерируется здесь
    )
    in_dlq = int(
        subprocess.run(
            [sys.executable, "-c", "import redis;print(redis.Redis(port=6390).xlen('anpr:dlq'))"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or 0
    )
    unique_accepted = accepted - duplicates  # дубликаты возвращают тот же ответ и не создают второй записи
    lost = max(0, unique_accepted - in_db - in_dlq - offline_left)
    duplicate_sessions = int(
        _psql(
            db_url,
            "SELECT count(*) FROM (SELECT lot_id, vehicle_id FROM parking_session WHERE status = 'ACTIVE' GROUP BY 1, 2 HAVING count(*) > 1) d",
        )
        or 0
    )
    sessions_created = int(
        _psql(db_url, f"SELECT count(*) FROM parking_session WHERE created_at >= to_timestamp({started})") or 0  # noqa: S608 — float времени
    )

    s = sampler.samples
    first_b = _buckets(s.first_metrics, "exit_end_to_end_seconds", 'paid="no"')
    last_b = _buckets(s.last_metrics, "exit_end_to_end_seconds", 'paid="no"')
    delta = {le: last_b.get(le, 0) - first_b.get(le, 0) for le in last_b}
    exit_p95 = _quantile(delta, 0.95)
    exit_count = int(delta.get(float("inf"), 0)) if delta else 0
    rss_growth = {name: (vals[-1] - vals[0]) / vals[0] if vals and vals[0] else 0.0 for name, vals in s.rss_kb.items()}
    outbox_monotonic = (
        len(s.outbox_pending) > 3
        and all(b >= a for a, b in zip(s.outbox_pending, s.outbox_pending[1:], strict=False))
        and s.outbox_pending[-1] > s.outbox_pending[0]
    )
    max_lag = max(s.replica_lag) if s.replica_lag else None

    results = {
        "scenario": args.scenario,
        "run_id": run_id,
        "scale": args.scale,
        "rate_scale": args.rate_scale,
        "workers": args.workers,
        "duration_s": round(elapsed, 1),
        "requests": requests,
        "accepted": accepted,
        "queued_202": queued,
        "duplicates": duplicates,
        "rejected_4xx": rejected,
        "failed_5xx": failed,
        "camera_p95_ms": round(p95, 1),
        "camera_p99_ms": round(p99, 1),
        "camera_error_rate": error_rate,
        "heartbeat_p95_ms": round(float(m.get("http_req_duration{name:heartbeat}", {}).get("p(95)", 0)), 1),
        "operator_api_p95_ms": round(float(m.get("http_req_duration{name:operator_api}", {}).get("p(95)", 0)), 1),
        "ws_messages": int(metric("ws_messages", "count")),
        "in_db": in_db,
        "in_dlq": in_dlq,
        "offline_left": offline_left,
        "lost": lost,
        "duplicate_sessions": duplicate_sessions,
        "sessions_created": sessions_created,
        "exit_e2e_p95_s": None if exit_p95 is None else round(exit_p95, 2),
        "exit_e2e_count": exit_count,
        "outbox_pending_series": s.outbox_pending,
        "outbox_monotonic_growth": outbox_monotonic,
        "db_connections_max": max(s.db_connections) if s.db_connections else None,
        "redis_latency_p95_ms": round(sorted(s.redis_latency_ms)[int(0.95 * (len(s.redis_latency_ms) - 1))], 2)
        if s.redis_latency_ms
        else None,
        "wal_mb": round((s.wal_bytes[-1] if s.wal_bytes else 0) / 1e6, 1),
        "rss_growth": {k: round(v, 3) for k, v in rss_growth.items()},
        "rss_kb_end": {k: v[-1] for k, v in s.rss_kb.items() if v},
        "replica_lag_max_s": max_lag,
        "stream_lag_max": int(max(s.stream_lag)) if s.stream_lag else None,
        "stream_lag_end": int(s.stream_lag[-1]) if s.stream_lag else None,
        "consumer_dlq_total": int(
            sum(v for k, v in s.last_metrics.items() if k.startswith("dlq_messages_total{"))
            - sum(v for k, v in s.first_metrics.items() if k.startswith("dlq_messages_total{"))
        ),
        "saga_manual_review": int(
            _psql(
                db_url,
                f"SELECT count(*) FROM exit_saga WHERE state = 'MANUAL_REVIEW' AND created_at >= to_timestamp({started})",  # noqa: S608 — float времени
            )
            or 0
        ),
        "k6_thresholds_passed": k6.returncode == 0,
    }
    verdict = {
        "p95 < 150 ms": p95 < CRITERIA["p95_ms"],
        "p99 < 400 ms": p99 < CRITERIA["p99_ms"],
        "error rate < 0.1 %": error_rate < CRITERIA["error_rate"] and failed == 0,
        "EXIT e2e p95 < 2 s": exit_p95 is not None and exit_p95 < CRITERIA["exit_e2e_p95_s"],
        "duplicate sessions = 0": duplicate_sessions == 0,
        "lost events = 0 (sent == db + dlq)": lost == 0,
        "RSS growth < 10 %": all(v < CRITERIA["rss_growth"] for v in rss_growth.values()),
        "outbox_pending not monotonic": not outbox_monotonic,
        "replica lag < 5 s": max_lag is None or max_lag < CRITERIA["replica_lag_s"],
    }
    results["verdict"] = verdict
    (REPORT_DIR / f"{args.scenario}.json").write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    lines = [
        f"# k6 — сценарий `{args.scenario}` ({run_id})",
        "",
        f"SCALE=1/{args.scale} (длительность {elapsed:.0f} с), RATE_SCALE=1/{args.rate_scale} (интенсивности §6 поделены), uvicorn workers={args.workers}, стенд: {'лаборатория primary+sync standby' if args.lab else 'pgserver'}; k6 {subprocess.run([str(K6), 'version'], capture_output=True, text=True, check=False).stdout.split()[1] if K6.exists() else ''}.",
        "",
        "| Метрика | Значение | Критерий | Статус |",
        "|---|---|---|---|",
        f"| POST /camera/events p95 | {p95:.1f} ms | < 150 ms | {'✅' if verdict['p95 < 150 ms'] else '❌'} |",
        f"| POST /camera/events p99 | {p99:.1f} ms | < 400 ms | {'✅' if verdict['p99 < 400 ms'] else '❌'} |",
        f"| error rate (camera events) | {error_rate * 100:.3f} % (5xx/сеть: {failed}) | < 0.1 % | {'✅' if verdict['error rate < 0.1 %'] else '❌'} |",
        f"| EXIT end-to-end p95 (без оплаты, n={exit_count}) | {results['exit_e2e_p95_s']} s | < 2 s | {'✅' if verdict['EXIT e2e p95 < 2 s'] else '❌'} |",
        f"| дубликаты сессий | {duplicate_sessions} | 0 | {'✅' if verdict['duplicate sessions = 0'] else '❌'} |",
        f"| потерянные события: принято {accepted} (дублей {duplicates}, 202: {queued}) = в БД {in_db} + DLQ {in_dlq} + буфер {offline_left} | потеряно {lost} | 0 | {'✅' if verdict['lost events = 0 (sent == db + dlq)'] else '❌'} |",
        f"| рост RSS | {results['rss_growth']} | < 10 % | {'✅' if verdict['RSS growth < 10 %'] else '❌'} |",
        f"| outbox_pending под нагрузкой | {[int(x) for x in s.outbox_pending][:40]} | не растёт монотонно | {'✅' if verdict['outbox_pending not monotonic'] else '❌'} |",
        f"| лаг репликации | {max_lag if max_lag is not None else 'n/a (нет реплики)'} s | < 5 s | {'✅' if verdict['replica lag < 5 s'] else '❌'} |",
        "",
        f"Запросов всего {requests}; heartbeat p95 {results['heartbeat_p95_ms']} ms; operator API p95 {results['operator_api_p95_ms']} ms; WS-сообщений {results['ws_messages']}; "
        f"соединений БД max {results['db_connections_max']}; Redis PING p95 {results['redis_latency_p95_ms']} ms; WAL за прогон {results['wal_mb']} MB; RSS в конце (KB) {results['rss_kb_end']}; "
        f"лаг стримов (pending) max {results['stream_lag_max']}, в конце {results['stream_lag_end']}; DLQ consumer'а за прогон {results['consumer_dlq_total']}; саг в MANUAL_REVIEW {results['saga_manual_review']}.",
        "",
        f"k6 thresholds: {'passed' if results['k6_thresholds_passed'] else 'FAILED'}; лог — `docs/backend/load/{args.scenario}-k6.log`, полные данные — `{args.scenario}.json`.",
    ]
    (REPORT_DIR / f"{args.scenario}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    if not args.keep:
        subprocess.run(
            [sys.executable, str(BACKEND / "scripts" / "e2e_stack.py"), "stop"], check=False, capture_output=True
        )
        if lab is not None:
            lab.stop()
    return 0 if all(verdict.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
