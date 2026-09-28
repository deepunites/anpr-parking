#!/usr/bin/env python3
"""Генератор дашбордов Grafana (фаза 7 §4). Источник истины — этот файл; JSON в dashboards/ — артефакт, который
провижинится (provisioning/dashboards/parking.yaml, Helm ConfigMap с меткой grafana_dashboard="1").

    python3 deploy/grafana/gen_dashboards.py          # перегенерировать deploy/grafana/dashboards/*.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "dashboards"
HELM_OUT = Path(__file__).resolve().parents[1] / "helm" / "parking" / "dashboards"  # копия для .Files.Glob чарта
DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
LOT = 'lot=~"$lot"'


def target(expr: str, legend: str = "{{lot}}") -> dict[str, Any]:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A"}


def panel(
    title: str,
    exprs: list[tuple[str, str]],
    *,
    kind: str = "timeseries",
    unit: str = "short",
    w: int = 12,
    h: int = 8,
    description: str = "",
    thresholds: list[tuple[str, float | None]] | None = None,
    max_value: float | None = None,
) -> dict[str, Any]:
    targets = [dict(target(e, legend), refId=chr(ord("A") + i)) for i, (e, legend) in enumerate(exprs)]
    steps = [{"color": c, "value": v} for c, v in (thresholds or [("green", None)])]
    field: dict[str, Any] = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if max_value is not None:
        field["max"] = max_value
        field["min"] = 0
    return {
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": targets,
        "gridPos": {"w": w, "h": h, "x": 0, "y": 0},  # x/y расставляет layout()
        "fieldConfig": {"defaults": field, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
        if kind == "timeseries"
        else {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"},
    }


def layout(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    x = y = row_h = 0
    for i, p in enumerate(panels):
        w, h = p["gridPos"]["w"], p["gridPos"]["h"]
        if x + w > 24:
            x, y, row_h = 0, y + row_h, 0
        p["gridPos"].update(x=x, y=y)
        p["id"] = i + 1
        x += w
        row_h = max(row_h, h)
    return panels


def dashboard(uid: str, title: str, description: str, panels: list[dict[str, Any]], *, lot_var: bool = True) -> dict[str, Any]:
    templating = [
        {
            "name": "DS_PROMETHEUS",
            "type": "datasource",
            "query": "prometheus",
            "label": "Prometheus",
            "current": {"text": "Prometheus", "value": "Prometheus"},
        }
    ]
    if lot_var:
        templating.append(
            {
                "name": "lot",
                "type": "query",
                "label": "Площадка",
                "datasource": DS,
                "query": "label_values(session_active, lot)",
                "refresh": 2,
                "includeAll": True,
                "multi": True,
                "current": {"text": "All", "value": "$__all"},
            }
        )
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["parking", uid.split("-")[-1]],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "graphTooltip": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": templating},
        "annotations": {
            "list": [
                {
                    "name": "Deploys",
                    "datasource": DS,
                    "expr": 'changes(process_start_time_seconds{job="parking-api"}[5m]) > 0',
                    "enable": True,
                    "iconColor": "blue",
                    "titleFormat": "deploy",
                }
            ]
        },
        "panels": layout(panels),
    }


RED = [("green", None), ("red", 1.0)]


def operations() -> dict[str, Any]:
    return dashboard(
        "parking-operations",
        "Parking — Operations",
        "Здоровье площадок: занятость, события, отказы шлагбаумов, оффлайн-камеры. Для оператора и поддержки.",
        [
            panel("Занятость площадок", [(f"occupancy_ratio{{{LOT}}}", "{{lot}}")], unit="percentunit", w=8, kind="gauge", max_value=1.2, thresholds=[("green", None), ("orange", 0.85), ("red", 1.0)]),
            panel("Активных сессий", [(f"session_active{{{LOT}}}", "{{lot}}")], w=8),
            panel("Камеры не ONLINE", [(f'sum by (lot) (camera_status_by_lot{{{LOT},status!="ONLINE"}})', "{{lot}}")], w=8, kind="stat", thresholds=RED, description="OFFLINE/SILENT/DEGRADED/UNKNOWN по площадкам (алерты CameraOffline, LotWithoutCameras)"),
            panel("ENTRY по исходу, /мин", [(f"sum by (result) (rate(entry_total{{{LOT}}}[5m])) * 60", "{{result}}")], unit="cpm"),
            panel("EXIT по исходу, /мин", [(f"sum by (result) (rate(exit_total{{{LOT}}}[5m])) * 60", "{{result}}")], unit="cpm"),
            panel("Команды шлагбаумов по результату, /мин", [(f"sum by (lot, result) (rate(barrier_command_total{{{LOT}}}[5m])) * 60", "{{lot}} {{result}}")], unit="cpm"),
            panel("Доля успешных OPEN (SLO 99.5 %)", [(f'sum(rate(barrier_command_total{{{LOT},command="OPEN",result="OK"}}[10m])) / sum(rate(barrier_command_total{{{LOT},command="OPEN"}}[10m]))', "OPEN ok")], unit="percentunit", thresholds=[("red", None), ("orange", 0.98), ("green", 0.995)]),
            panel("Отклонённые события камер по причине", [("sum by (reason) (rate(camera_event_rejected_total[5m])) * 60", "{{reason}}")], unit="cpm"),
            panel("Сдвиг часов камер (p95 по площадке)", [(f"histogram_quantile(0.95, sum by (lot, le) (rate(camera_clock_skew_seconds_bucket{{{LOT}}}[15m])))", "{{lot}}")], unit="s"),
            panel("Камеры по статусу (всего)", [("camera_status_total", "{{status}}")], w=8),
            panel("Оффлайн-режим API / буфер", [("max(offline_mode_active)", "offline_mode_active"), ("max(offline_queue_length)", "anpr:offline length")], w=8, thresholds=RED),
            panel("Ручные коррекции номеров, /ч", [(f"sum by (lot) (increase(manual_correction_total{{{LOT}}}[1h]))", "{{lot}}")], w=8),
            panel("Решения доступа", [("sum by (kind) (rate(access_decision_total[5m])) * 60", "{{kind}}")], unit="cpm"),
            panel("Провайдеры платежей: результат вызовов", [("sum by (provider, result) (rate(gateway_calls_total[5m])) * 60", "{{provider}} {{result}}")], unit="cpm"),
        ],
    )


def slo() -> dict[str, Any]:
    cam = 'route="/api/v1/camera/events"'
    return dashboard(
        "parking-slo",
        "Parking — SLO",
        "Четыре SLI из §4 CORE + доступность API: текущие значения, расход error budget, тренд.",
        [
            panel("Camera events p95 (SLO < 150 мс, алерт > 250 мс)", [(f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{cam}}}[5m])))", "p95"), (f"histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{{cam}}}[5m])))", "p99")], unit="s", thresholds=[("green", None), ("orange", 0.15), ("red", 0.25)]),
            panel("Camera events: доля 5xx (SLO < 0.1 %)", [(f'sum(rate(http_request_duration_seconds_count{{{cam},status_class="5xx"}}[5m])) / sum(rate(http_request_duration_seconds_count{{{cam}}}[5m]))', "5xx ratio")], unit="percentunit", thresholds=[("green", None), ("orange", 0.001), ("red", 0.005)]),
            panel("EXIT end-to-end p95 без оплаты (SLO < 2 с)", [('histogram_quantile(0.95, sum by (le) (rate(exit_end_to_end_seconds_bucket{paid="no"}[5m])))', "p95 no payment"), ('histogram_quantile(0.95, sum by (le) (rate(exit_end_to_end_seconds_bucket{paid="yes"}[5m])))', "p95 with payment")], unit="s", thresholds=[("green", None), ("orange", 2), ("red", 4)]),
            panel("Открытие шлагбаума: success rate (SLO > 99.5 %)", [('sum(rate(barrier_command_total{command="OPEN",result="OK"}[10m])) / sum(rate(barrier_command_total{command="OPEN"}[10m]))', "success")], unit="percentunit", thresholds=[("red", None), ("orange", 0.98), ("green", 0.995)]),
            panel("Подтверждение платежа p95 (SLO < 5 с)", [("histogram_quantile(0.95, sum by (le) (rate(payment_confirm_latency_seconds_bucket[10m])))", "p95")], unit="s", thresholds=[("green", None), ("orange", 5), ("red", 10)]),
            panel("Доступность API (30 дней, SLO 99.9 %)", [('1 - sum(rate(http_request_duration_seconds_count{status_class="5xx"}[30d])) / sum(rate(http_request_duration_seconds_count[30d]))', "availability")], kind="stat", unit="percentunit", thresholds=[("red", None), ("orange", 0.999), ("green", 0.9995)]),
            panel("Error budget израсходовано (30 дней)", [('(sum(rate(http_request_duration_seconds_count{status_class="5xx"}[30d])) / sum(rate(http_request_duration_seconds_count[30d]))) / 0.001', "budget used")], kind="gauge", unit="percentunit", max_value=1.0, thresholds=[("green", None), ("orange", 0.5), ("red", 1.0)]),
            panel("Burn rate (1h / 6h)", [('(sum(rate(http_request_duration_seconds_count{status_class="5xx"}[1h])) / sum(rate(http_request_duration_seconds_count[1h]))) / 0.001', "1h"), ('(sum(rate(http_request_duration_seconds_count{status_class="5xx"}[6h])) / sum(rate(http_request_duration_seconds_count[6h]))) / 0.001', "6h")], thresholds=[("green", None), ("orange", 6), ("red", 14.4)]),
            panel("Латентность по роутам p95", [("histogram_quantile(0.95, sum by (route, le) (rate(http_request_duration_seconds_bucket[5m])))", "{{route}}")], unit="s", w=24),
        ],
        lot_var=False,
    )


def pipeline() -> dict[str, Any]:
    return dashboard(
        "parking-pipeline",
        "Parking — Pipeline",
        "Outbox, Redis Streams, DLQ, лаг консьюмеров, репликация PostgreSQL, очереди Celery, зависимости.",
        [
            panel("Зависимости (1 = ок)", [("min by (dependency) (dependency_up)", "{{dependency}}")], kind="stat", w=8, thresholds=[("red", None), ("green", 1)]),
            panel("Outbox: PENDING", [("max(outbox_pending_total)", "pending")], kind="stat", w=8, thresholds=[("green", None), ("orange", 100), ("red", 1000)]),
            panel("Outbox: возраст самого старого PENDING", [("max(outbox_oldest_pending_age_seconds)", "age")], kind="stat", unit="s", w=8, thresholds=[("green", None), ("orange", 10), ("red", 30)]),
            panel("Outbox: публикаций / ошибок, /мин", [("sum(rate(outbox_published_total[5m])) * 60", "published"), ("sum(rate(outbox_failed_total[5m])) * 60", "failed")], unit="cpm"),
            panel("Outbox: лаг публикации и время батча", [("max(outbox_publish_lag_seconds)", "publish lag"), ("histogram_quantile(0.95, sum by (le) (rate(outbox_batch_seconds_bucket[5m])))", "batch p95")], unit="s"),
            panel("Стримы: отставание по шардам (lag + pending)", [("stream_pending_total", "{{stream}}")], thresholds=[("green", None), ("orange", 100), ("red", 1000)]),
            panel("DLQ: длина и приток", [("dlq_length", "{{stream}} length"), ("sum by (stream) (increase(dlq_messages_total[15m]))", "{{stream}} +15m")], thresholds=RED),
            panel("Consumer: обработано по типу, /мин", [("sum by (event_type) (rate(consumer_processed_total[5m])) * 60", "{{event_type}}")], unit="cpm"),
            panel("Consumer: ошибки, /мин", [('sum by (event_type) (rate(consumer_processed_total{result="error"}[5m])) * 60', "{{event_type}}")], unit="cpm", thresholds=RED),
            panel("Саги по состояниям, /мин", [("sum by (state) (rate(saga_steps_total[5m])) * 60", "{{state}}")], unit="cpm"),
            panel("Репликация PostgreSQL: лаг", [("max(pg_replication_lag_seconds)", "lag")], unit="s", thresholds=[("green", None), ("orange", 5), ("red", 30)], description="postgres_exporter"),
            panel("Celery: очереди", [("celery_queue_length", "{{queue_name}}")], description="celery-exporter (фаза 9); до него — redis-cli LLEN saga/barrier/payments/maintenance"),
            panel("Оффлайн-буфер ANPR", [("max(offline_queue_length)", "anpr:offline"), ('sum(rate(offline_replayed_total{result="ok"}[5m])) * 60', "replayed/min")]),
            panel("WebSocket", [("sum(ws_connections)", "connections"), ("sum(rate(ws_dropped_total[5m])) * 60", "dropped/min"), ("sum(rate(ws_overloaded_total[5m])) * 60", "overloaded/min")]),
        ],
        lot_var=False,
    )


def business() -> dict[str, Any]:
    return dashboard(
        "parking-business",
        "Parking — Business",
        "Выручка по способам оплаты, длительность сессий, льготы, ручные коррекции, расхождения сверки. "
        "revenue_total — операционный индикатор (float64), финансовая истина — PostgreSQL.",
        [
            panel("Выручка за сутки по способу оплаты (тийин → сум)", [("sum by (method) (increase(revenue_total[24h])) / 100", "{{method}}")], kind="stat", unit="currencyUZS", w=12),
            panel("Выручка, сум/ч", [("sum by (method) (rate(revenue_total[1h])) * 3600 / 100", "{{method}}")], unit="currencyUZS"),
            panel("Платежи по статусам, /ч", [("sum by (method, status) (increase(payment_total[1h]))", "{{method}} {{status}}")]),
            panel("Подтверждение платежа p50/p95", [("histogram_quantile(0.5, sum by (le) (rate(payment_confirm_latency_seconds_bucket[1h])))", "p50"), ("histogram_quantile(0.95, sum by (le) (rate(payment_confirm_latency_seconds_bucket[1h])))", "p95")], unit="s"),
            panel("Льготы и абонементы: решения доступа, /ч", [('sum by (kind) (increase(access_decision_total{kind!="NORMAL"}[1h]))', "{{kind}}")]),
            panel("Ручные коррекции номеров, /сут", [(f"sum by (lot) (increase(manual_correction_total{{{LOT}}}[24h]))", "{{lot}}")]),
            panel("Открытые расхождения сверки по виду", [("reconciliation_discrepancies_open", "{{kind}}")], thresholds=[("green", None), ("orange", 5), ("red", 10)]),
            panel("Возвраты, /сут", [("sum by (method, result) (increase(refund_total[24h]))", "{{method}} {{result}}")]),
            panel("Въезды / выезды за сутки", [(f'sum(increase(entry_total{{{LOT},result="ENTRY_CONFIRMED"}}[24h]))', "entries"), (f'sum(increase(exit_total{{{LOT},result=~"EXIT_CONFIRMED|PAYMENT_REQUIRED"}}[24h]))', "exits")], kind="stat"),
            panel("Вебхуки провайдеров по результату, /ч", [("sum by (provider, result) (increase(webhook_total[1h]))", "{{provider}} {{result}}")]),
        ],
    )


def main() -> None:
    for out in (OUT, HELM_OUT):
        out.mkdir(parents=True, exist_ok=True)
    for build in (operations, slo, pipeline, business):
        data = build()
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        for out in (OUT, HELM_OUT):
            (out / f"{build.__name__}.json").write_text(text)
        print(f"{build.__name__}.json: {len(data['panels'])} panels")


if __name__ == "__main__":
    main()
