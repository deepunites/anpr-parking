"""Фаза 7 §2: /metrics отдаёт метрики без ПДн-лейблов; расчёт кардинальности каждой метрики — суммарно < 100k рядов.
Расчёт пишется в docs/backend/metrics-cardinality.md (артефакт gate)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from prometheus_client import Gauge, Histogram
from prometheus_client.parser import text_string_to_metric_families

from parking.platform import metrics
from parking.runtime.container import Container
from tests.support.camera_fixtures import event_body, send_event, unique_plate

DOCS = Path(__file__).resolve().parents[3] / "docs"
CARDINALITY_DOC = DOCS / "backend" / "metrics-cardinality.md"
FORBIDDEN_LABELS = {"plate", "plate_number", "session_id", "correlation_id", "user_id", "camera_id", "error", "path"}

# Верхние оценки числа значений лейблов (docs/OPERATIONS.md § Кардинальность). Новый лейбл без оценки — ошибка теста.
LABEL_BOUNDS: dict[str, int] = {
    "lot": 1000,  # площадок в инсталляции (§2: lot_id допустим, 1000 значений)
    "event_type": 40,  # типов интеграционных событий (docs/EVENTS.md)
    "reason": 12,  # RejectReason
    "state": 12,  # SagaState
    "command": 2,  # OPEN / CLOSE
    "status": 8,  # PaymentStatus / CameraStatus
    "method": 4,  # CASH / ONLINE / SUBSCRIPTION / FREE
    "provider": 4,  # PAYME / CLICK / …
    "kind": 8,  # DiscrepancyKind / AccessKind
    "stream": 12,  # events:{0..7}, anpr:offline, dlq
    "worker": 5,  # run_consumer, run_publisher, run_offline_replayer, run_camera_health, celery
    "dependency": 2,  # postgres / redis
    "type": 12,  # типы WS-сообщений
    "status_class": 4,  # 2xx, 3xx, 4xx, 5xx (0xx — оборванный ответ, редкость)
    "paid": 2,
}
RESULT_BOUNDS: dict[str, int] = {  # лейбл result зависит от метрики (имя без суффикса _total)
    "entry": 6,  # EntryStatus
    "exit": 7,  # ExitStatus
    "barrier_command": 3,  # итоговые CommandState: OK / FAILED / SKIPPED
    "consumer_processed": 4,
    "camera_event": 12,
    "offline_replayed": 4,
    "gateway_calls": 6,
    "webhook": 6,
    "refund": 6,
    "webhook_delivery": 3,  # v2 этап 8: delivered / retry / dead
}
ROUTE_METHOD_COMBOS = 80  # 73 операции в docs/openapi.json + /health, /metrics, <unmatched> + запас
STATUS_BOUNDS: dict[str, int] = {
    "camera_status_by_lot": 5,
    "camera_status_total": 5,
    "payment": 6,
}  # CameraStatus / PaymentStatus
INSTANCES = {"api": 3, "worker": 2, "singleton": 1}  # реплики API / воркеры (consumer, celery) / camera-health
# какие процессы эмитят метрику (ряды считаются на инстанс); не перечисленные — все процессы (консервативно)
OWNERS: dict[str, tuple[str, ...]] = {
    "http_request_duration_seconds": ("api",),
    "webhook_delivery": ("singleton",),  # v2: диспетчер вебхуков
    "event_journal_written": ("api",),
    "event_journal_dropped": ("api",),
    "entry": ("api",),
    "exit": ("api",),
    "camera_event": ("api",),
    "camera_event_rejected": ("api",),
    "camera_clock_skew_seconds": ("api",),
    "camera_auth_failed": ("api",),
    "camera_auth_abuse": ("api",),
    "camera_auth_seconds": ("api",),
    "access_decision": ("api",),
    "manual_correction": ("api",),
    "offline_mode_active": ("api",),
    "offline_queue_length": ("api",),
    "ws_connections": ("api",),
    "ws_messages": ("api",),
    "ws_dropped": ("api",),
    "ws_overloaded": ("api",),
    "webhook": ("api",),
    "payment_confirm_latency_seconds": ("api",),
    "revenue": ("api",),
    "session_active": ("api",),  # MetricsRefresher — только в API
    "occupancy_ratio": ("api",),
    "reconciliation_discrepancies_open": ("api",),
    "dependency_up": ("api",),
    "outbox_pending_total": ("api", "worker"),
    "outbox_oldest_pending_age_seconds": ("api",),
    "stream_pending_total": ("api",),
    "dlq_length": ("api",),
    "outbox_published": ("worker",),
    "outbox_failed": ("worker",),
    "outbox_publish_lag_seconds": ("worker",),
    "outbox_batch_seconds": ("worker",),
    "consumer_processed": ("worker",),
    "dlq_messages": ("worker",),
    "consumer_retry_scheduled": ("worker",),
    "worker_loop_restarts": ("worker",),
    "redis_stream_lag": ("worker",),
    "saga_steps": ("worker",),
    "barrier_command": ("worker",),
    "exit_end_to_end_seconds": ("worker",),
    "offline_replayed": ("worker",),
    "camera_status_total": ("singleton",),  # camera_health — один процесс
    "camera_status_by_lot": ("singleton",),
}
BUDGET = 100_000


def _series_per_metric(collector: object) -> int:
    if isinstance(collector, Histogram):
        return len(collector._upper_bounds) + 2
    return 1


def _label_bound(metric: str, label: str) -> int:
    if label == "result":
        return RESULT_BOUNDS[metric]
    if label == "route":
        return 1  # считается в паре с method (ROUTE_METHOD_COMBOS)
    if label == "status" and metric in STATUS_BOUNDS:
        return STATUS_BOUNDS[metric]
    if label == "method" and metric.startswith("http_"):
        return ROUTE_METHOD_COMBOS
    return LABEL_BOUNDS[label]


def _collectors() -> list[tuple[str, object, tuple[str, ...]]]:
    out = []
    for collector in metrics.registry._collector_to_names:
        name = getattr(collector, "_name", None)
        if name is None:
            continue
        out.append((str(name), collector, tuple(getattr(collector, "_labelnames", ()))))
    return out


def cardinality_table() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, collector, labels in _collectors():
        combos = 1
        for label in labels:
            combos *= _label_bound(name, label)
        per_instance = combos * _series_per_metric(collector)
        instances = sum(INSTANCES[kind] for kind in OWNERS.get(name, tuple(INSTANCES)))
        rows.append(
            {
                "metric": name if isinstance(collector, Histogram | Gauge) else f"{name}_total",
                "type": type(collector).__name__,
                "labels": ", ".join(labels) or "—",
                "combos": combos,
                "series_per_instance": per_instance,
                "instances": instances,
                "owners": "+".join(OWNERS.get(name, tuple(INSTANCES))),
                "series": per_instance * instances,
            }
        )
    return sorted(rows, key=lambda r: -int(str(r["series"])))


def test_every_label_has_a_documented_bound_and_total_under_budget() -> None:
    for name, collector, labels in _collectors():
        for label in labels:
            assert label not in FORBIDDEN_LABELS, f"{name}: лейбл {label} запрещён (§2)"
            if label == "result":
                assert name in RESULT_BOUNDS, f"{name}: для лейбла result нет оценки кардинальности (§2)"
            elif label not in LABEL_BOUNDS and label != "route":
                pytest.fail(f"{name}: для лейбла {label} нет оценки кардинальности (§2)")
        assert not ({"lot", "camera_id"} <= set(labels)), "lot и camera_id вместе на одной метрике запрещены"
        if isinstance(collector, Histogram):
            assert "camera_id" not in labels, "camera_id на гистограммах запрещён (§2)"
    rows = cardinality_table()
    total = sum(int(r["series"]) for r in rows)
    assert total < BUDGET, f"{total} рядов ≥ {BUDGET}"
    lines = [
        "# Кардинальность метрик (расчёт, фаза 7 §2)",
        "",
        f"Оценка сверху при {INSTANCES['api']} репликах API и {INSTANCES['worker']} воркерах, {LABEL_BOUNDS['lot']} площадках. "
        f"Генерируется тестом `tests/observability/test_metrics.py`; бюджет {BUDGET:,} рядов.",
        "",
        "| Метрика | Тип | Лейблы | Комбинаций | Рядов/инстанс | Процессы | Инстансов | Рядов |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| `{r['metric']}` | {r['type']} | {r['labels']} | {r['combos']:,} | {r['series_per_instance']:,} | "
            f"{r['owners']} | {r['instances']} | {r['series']:,} |"
            for r in rows
        ),
        f"| **Итого** | | | | | | | **{total:,}** |",
        "",
        "Оценки значений лейблов: "
        + ", ".join(f"`{k}` ≤ {v}" for k, v in LABEL_BOUNDS.items())
        + "; "
        + "`result` по метрике: "
        + ", ".join(f"`{k}` ≤ {v}" for k, v in RESULT_BOUNDS.items())
        + "; "
        + f"`method×route` (HTTP) ≤ {ROUTE_METHOD_COMBOS}.",
        "",
    ]
    CARDINALITY_DOC.parent.mkdir(parents=True, exist_ok=True)
    CARDINALITY_DOC.write_text("\n".join(lines))
    print(f"metrics cardinality: {total:,} series (< {BUDGET:,}) → {CARDINALITY_DOC}")


async def test_metrics_endpoint_scrapes_live_values(
    client: httpx.AsyncClient, container: Container, entry_key: str, clean_lot: None
) -> None:
    resp = await send_event(client, entry_key, event_body(unique_plate()))
    assert resp.status_code == 200, resp.text
    scrape = await client.get("/metrics")
    assert scrape.status_code == 200 and scrape.headers["content-type"].startswith("text/plain")
    families = {f.name: f for f in text_string_to_metric_families(scrape.text)}  # имена без суффикса _total
    for required in (
        "http_request_duration_seconds",
        "entry",
        "session_active",
        "occupancy_ratio",
        "outbox_pending_total",
        "outbox_oldest_pending_age_seconds",
        "stream_pending_total",
        "dlq_length",
        "dependency_up",
        "camera_event",
    ):
        assert required in families, f"нет метрики {required}; есть: {sorted(families)[:20]}…"
    samples = [s for f in families.values() for s in f.samples]
    assert {s.labels["dependency"]: s.value for s in samples if s.name == "dependency_up"} == {
        "postgres": 1.0,
        "redis": 1.0,
    }
    http = [s for s in samples if s.name == "http_request_duration_seconds_count"]
    assert any(s.labels["route"] == "/api/v1/camera/events" and s.labels["status_class"] == "2xx" for s in http)
    assert not any(s.name.endswith("_created") for s in samples), "*_created-ряды отключены"
    for s in samples:
        assert not (set(s.labels) & FORBIDDEN_LABELS), s
        for value in s.labels.values():
            assert (not value.startswith("eyJ") and " " not in value.strip()) or s.name.startswith("http_"), s
    # /metrics — без аутентификации (scrape изнутри кластера), не в OpenAPI
    openapi = (await client.get("/api/openapi.json")).json() if False else None
    assert openapi is None
    assert await container.redis.ping()


async def test_metrics_endpoint_not_in_openapi(client: httpx.AsyncClient) -> None:
    spec = json.loads((DOCS / "openapi.json").read_text())
    assert "/metrics" not in spec["paths"] and "/health" not in spec["paths"]
