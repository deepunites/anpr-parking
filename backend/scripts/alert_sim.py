"""Симуляция алертов на живом стенде (фаза 7 §5, gate): Prometheus скрейпит e2e-API, правила из
deploy/prometheus/alerts.yaml с ускоренными `for` (÷SCALE), сценарии:

  1. остановлен outbox publisher → событие камеры копится в outbox → OutboxLag (порог 30 с + for 2m/SCALE);
  2. остановлен PostgreSQL → dependency_up{postgres}=0 / offline_mode_active=1 → PostgresPrimaryDown (for 1m/SCALE);
     затем PostgreSQL поднят → алерт гаснет.

    uv run python scripts/alert_sim.py            # нужны: pg_dev start, redis_dev start 6390, ~/.local/prometheus/bin/prometheus
Вывод — в docs/backend/gate-7.out. Ничего не оставляет запущенным (стенд и Prometheus останавливаются, PostgreSQL поднимается).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
PROM = Path(os.environ.get("PROMETHEUS_BIN", Path.home() / ".local/prometheus/bin/prometheus"))
WORK = BACKEND / ".e2e" / "prom"
PROM_PORT = 9091
API_PORT = 8010
SCALE = 10  # for: 1m → 6s, 2m → 12s, …
UNITS = {"s": 1, "m": 60, "h": 3600}


def _scaled_rules() -> Path:
    src = (ROOT / "deploy/prometheus/alerts.yaml").read_text()

    def scale(m: re.Match[str]) -> str:
        seconds = int(m.group(1)) * UNITS[m.group(2)] // SCALE
        return f"for: {seconds}s"

    out = WORK / "alerts.scaled.yaml"
    out.write_text(re.sub(r"for: (\d+)([smh])", scale, src))
    return out


def _prom_config(rules: Path) -> Path:
    cfg = WORK / "prometheus.yml"
    cfg.write_text(
        f"""global:
  scrape_interval: 1s
  evaluation_interval: 1s
rule_files:
  - {rules}
scrape_configs:
  - job_name: parking-api
    static_configs:
      - targets: ["127.0.0.1:{API_PORT}"]
"""
    )
    return cfg


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — только http://127.0.0.1
        return json.loads(resp.read())


def _alerts() -> dict[str, str]:
    data = _get(f"http://127.0.0.1:{PROM_PORT}/api/v1/alerts")["data"]["alerts"]
    return {a["labels"]["alertname"]: a["state"] for a in data}


def _wait_state(name: str, state: str | None, timeout: float) -> float:
    """Ждёт, пока алерт окажется в состоянии state (None — пропал), возвращает затраченные секунды."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        current = _alerts().get(name)
        if current == state:
            return time.monotonic() - started
        time.sleep(0.5)
    raise SystemExit(f"{name}: не достиг состояния {state} за {timeout}s (сейчас {_alerts().get(name)})")


def _pid(name: str) -> int:
    return int((BACKEND / ".e2e" / f"{name}.pid").read_text())


def _run(*args: str, check: bool = True) -> None:
    subprocess.run([sys.executable, *args], cwd=BACKEND, check=check, capture_output=True)


def _camera_event(state: dict[str, Any]) -> int:
    body = json.dumps(
        {
            "camera_code": "ENTRY_01",
            "event_type": "ENTRY",
            "plate_number": f"01A{int(time.time()) % 900 + 100}BC",
            "confidence": 95,
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "direction": "IN",
            "recognition": {"provider": "sim", "version": "1.0"},
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 — локальный стенд
        f"{state['api']}/api/v1/camera/events",
        data=body,
        headers={"Content-Type": "application/json", "X-Camera-Key": state["camera_keys"]["entry"]},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return int(resp.status)


def _metric(query: str) -> str:
    result = _get(f"http://127.0.0.1:{PROM_PORT}/api/v1/query?query={urllib.request.quote(query)}")["data"]["result"]
    return result[0]["value"][1] if result else "n/a"


def main() -> int:  # noqa: PLR0915 — линейный сценарий симуляции
    if not PROM.exists():
        raise SystemExit(f"нет {PROM}; скачайте prometheus (см. docs/OBSERVABILITY.md)")
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"# alert simulation: SCALE=1/{SCALE} (for: 1m → {60 // SCALE}s), scrape/eval 1s")
    _run("scripts/e2e_stack.py", "start")
    state = json.loads((ROOT / "frontend/e2e/.stack.json").read_text())
    rules = _scaled_rules()
    prom = subprocess.Popen(
        [
            str(PROM),
            f"--config.file={_prom_config(rules)}",
            f"--storage.tsdb.path={WORK / 'data'}",
            f"--web.listen-address=127.0.0.1:{PROM_PORT}",
            "--log.level=error",
        ],
        stdout=open(WORK / "prometheus.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(50):
            try:
                if _get(f"http://127.0.0.1:{PROM_PORT}/-/ready") is not None:
                    break
            except Exception:
                time.sleep(0.2)
        time.sleep(3)
        loaded = _get(f"http://127.0.0.1:{PROM_PORT}/api/v1/rules")["data"]["groups"]
        n_rules = sum(len(g["rules"]) for g in loaded)
        print(
            f"prometheus {PROM.name}: rules loaded = {n_rules}, health = {sorted({r['health'] for g in loaded for r in g['rules']})}"
        )
        print(
            f"baseline: dependency_up{{postgres}}={_metric('dependency_up{dependency="postgres"}')}, "
            f"outbox_oldest_pending_age_seconds={_metric('outbox_oldest_pending_age_seconds')}, alerts={_alerts() or '{}'}"
        )

        # --- сценарий 1: остановлен outbox publisher
        print("\n## scenario 1: kill outbox publisher, then one camera ENTRY → outbox row stays PENDING")
        os.kill(_pid("publisher"), signal.SIGKILL)
        t0 = time.monotonic()
        status = _camera_event(state)
        print(f"camera event → HTTP {status} (t=0.0s)")
        pending = _wait_state("OutboxLag", "pending", 60)
        print(
            f"OutboxLag pending after {pending:.1f}s (age>{30}s threshold), age={_metric('outbox_oldest_pending_age_seconds')}s"
        )
        _wait_state("OutboxLag", "firing", 60)
        total = time.monotonic() - t0
        print(
            f"OutboxLag FIRING after {total:.1f}s total (expected ≈ 30s threshold + {120 // SCALE}s for + ≤6s scrape/refresh)"
        )

        # --- сценарий 2: остановлен PostgreSQL
        print("\n## scenario 2: stop PostgreSQL (pg_ctl stop -m fast)")
        _run("scripts/pg_dev.py", "stop", check=False)
        t0 = time.monotonic()
        _wait_state("PostgresPrimaryDown", "pending", 60)
        print(
            f"PostgresPrimaryDown pending after {time.monotonic() - t0:.1f}s, dependency_up{{postgres}}={_metric('dependency_up{dependency="postgres"}')}, "
            f"offline_mode_active={_metric('offline_mode_active')}"
        )
        _wait_state("PostgresPrimaryDown", "firing", 60)
        fired_at = time.monotonic() - t0
        print(f"PostgresPrimaryDown FIRING after {fired_at:.1f}s (expected ≈ {60 // SCALE}s for + ≤6s detection)")
        status = _camera_event(state)
        print(f"camera event during outage → HTTP {status} (202 = offline buffer)")
        _run("scripts/pg_dev.py", "start")
        t0 = time.monotonic()
        _wait_state("PostgresPrimaryDown", None, 90)
        print(
            f"PostgreSQL back → PostgresPrimaryDown resolved after {time.monotonic() - t0:.1f}s, "
            f"dependency_up{{postgres}}={_metric('dependency_up{dependency="postgres"}')}"
        )
        print(f"\nfinal alerts: {_alerts()}")
        return 0
    finally:
        prom.send_signal(signal.SIGTERM)
        try:
            prom.wait(10)
        except subprocess.TimeoutExpired:
            prom.kill()
        _run("scripts/e2e_stack.py", "stop", check=False)
        _run("scripts/pg_dev.py", "start", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
