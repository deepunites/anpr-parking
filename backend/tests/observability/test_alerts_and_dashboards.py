"""Фаза 7 §4–5: алерты полны (severity/summary/description/impact/runbook_url/for), runbook_url ведёт в существующий
раздел RUNBOOK.md, выражения ссылаются на существующие метрики; 4 дашборда — валидный JSON с известными метриками;
копии в Helm-чарте совпадают с источниками."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from parking.platform import metrics

ROOT = Path(__file__).resolve().parents[3]
ALERTS = ROOT / "deploy" / "prometheus" / "alerts.yaml"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
DASHBOARDS = ROOT / "deploy" / "grafana" / "dashboards"
HELM = ROOT / "deploy" / "helm" / "parking"

REQUIRED_ANNOTATIONS = ("summary", "description", "impact", "runbook_url")
SEVERITIES = {"critical", "warning", "info"}
# Обязательный список §5: critical и warning
REQUIRED_ALERTS = {
    "ApiDown",
    "PostgresPrimaryDown",
    "PostgresReplicaLag",
    "PaymentsFailing",
    "DlqGrowing",
    "LotWithoutCameras",
    "BackupMissing",
    "CertificateExpiringSoon",  # critical
    "CameraOffline",
    "BarrierFailure",
    "ReconciliationBacklog",
    "OutboxLag",
    "DiskUsageHigh",  # warning
    "ExitSuccessRatioLow",  # симптомный (§5)
}
# Метрики внешних экспортеров (postgres_exporter, blackbox, node_exporter, pushgateway, celery-exporter, Prometheus)
EXTERNAL_METRICS = {
    "up",
    "pg_up",
    "pg_replication_lag_seconds",
    "parking_backup_last_success_timestamp_seconds",
    "backup_restore_verified_timestamp",  # textfile collector verify-ВМ (фаза 9 §4)
    "probe_ssl_earliest_cert_expiry",
    "node_filesystem_size_bytes",
    "node_filesystem_avail_bytes",
    "celery_queue_length",
    "process_start_time_seconds",
}
PROMQL_WORDS = {
    "sum",
    "rate",
    "irate",
    "increase",
    "by",
    "on",
    "or",
    "and",
    "unless",
    "max",
    "min",
    "count",
    "avg",
    "le",
    "histogram_quantile",
    "absent",
    "time",
    "vector",
    "changes",
    "delta",
    "without",
    "bool",
    "offset",
    "ignoring",
    "group_left",
    "group_right",
    "label_values",
    "predict_linear",
    "e",
    "d",
    "h",
    "m",
    "s",
    "humanizePercentage",
    "humanizeDuration",
    "value",
    "labels",
    "job",
    "lot",
    "route",
    "status",
    "result",
    "command",
    "paid",
    "provider",
    "method",
    "stream",
    "dependency",
    "kind",
    "reason",
    "state",
    "event_type",
    "fstype",
    "instance",
    "mountpoint",
    "status_class",
    "queue_name",
    "tmpfs",
    "overlay",
    "ONLINE",
    "OFFLINE",
    "SILENT",
    "OK",
    "FAILED",
    "OPEN",
    "no",
    "yes",
    "ok",
    "PAID",
    "NORMAL",
    "ENTRY_CONFIRMED",
    "EXIT_CONFIRMED",
    "PAYMENT_REQUIRED",
    "parking",
    "error",
}
SUFFIXES = ("_total", "_bucket", "_count", "_sum")


def _registry_names() -> set[str]:
    names: set[str] = set()
    for collector in metrics.registry._collector_to_names:
        name = getattr(collector, "_name", None)
        if name:
            names.add(str(name))
    return names


def _metric_tokens(expr: str) -> set[str]:
    expr = re.sub(r'"[^"]*"', '""', expr)  # строковые литералы
    expr = re.sub(r"\$__?[a-zA-Z_]+|\$[a-zA-Z_]+", "", expr)  # переменные Grafana
    tokens = set(re.findall(r"(?<![\w{,])([a-zA-Z_][a-zA-Z0-9_]*)(?![\w=~!])", expr))
    return {t for t in tokens if t not in PROMQL_WORDS and not t.isupper()}


def _known(token: str, names: set[str]) -> bool:
    if token in EXTERNAL_METRICS or token in names:
        return True
    for suffix in SUFFIXES:
        if token.endswith(suffix) and token.removesuffix(suffix) in names:
            return True
    return False


@pytest.fixture(scope="module")
def rules() -> list[dict[str, object]]:
    data = yaml.safe_load(ALERTS.read_text())
    return [rule for group in data["groups"] for rule in group["rules"]]


def test_every_alert_is_complete(rules: list[dict[str, object]]) -> None:
    names = {str(r["alert"]) for r in rules}
    assert REQUIRED_ALERTS <= names, f"нет обязательных алертов: {REQUIRED_ALERTS - names}"
    for rule in rules:
        alert = rule["alert"]
        assert rule.get("expr") and "for" in rule, f"{alert}: expr/for"
        assert rule["labels"]["severity"] in SEVERITIES, alert  # type: ignore[index]
        annotations = rule["annotations"]  # type: ignore[index]
        for key in REQUIRED_ANNOTATIONS:
            assert str(annotations.get(key, "")).strip(), f"{alert}: пустая аннотация {key}"  # type: ignore[union-attr]
        assert "\n" not in str(annotations["summary"]).strip(), f"{alert}: summary — одна строка"  # type: ignore[index]


def test_runbook_urls_point_to_existing_sections(rules: list[dict[str, object]]) -> None:
    anchors = set(re.findall(r'<a id="([a-z0-9-]+)"></a>', RUNBOOK.read_text()))
    for rule in rules:
        url = str(rule["annotations"]["runbook_url"])  # type: ignore[index]
        assert "RUNBOOK.md#" in url, url
        anchor = url.split("#", 1)[1]
        assert anchor in anchors, f"{rule['alert']}: раздела #{anchor} нет в docs/RUNBOOK.md"


def test_alert_expressions_use_known_metrics(rules: list[dict[str, object]]) -> None:
    names = _registry_names()
    for rule in rules:
        unknown = {t for t in _metric_tokens(str(rule["expr"])) if not _known(t, names)}
        assert not unknown, f"{rule['alert']}: неизвестные метрики {unknown}"


@pytest.mark.skipif(
    shutil.which("promtool") is None and not Path.home().joinpath(".local/prometheus/bin/promtool").exists(),
    reason="promtool не установлен",
)
def test_promtool_check_rules() -> None:
    promtool = shutil.which("promtool") or str(Path.home() / ".local/prometheus/bin/promtool")
    result = subprocess.run([promtool, "check", "rules", str(ALERTS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "SUCCESS" in result.stdout, result.stdout + result.stderr
    print(result.stdout.strip())


def test_dashboards_are_valid_and_reference_known_metrics() -> None:
    files = sorted(DASHBOARDS.glob("*.json"))
    assert {f.stem for f in files} == {"operations", "slo", "pipeline", "business"}
    names = _registry_names()
    uids: set[str] = set()
    for file in files:
        data = json.loads(file.read_text())
        assert data["uid"] not in uids and data["title"] and data["panels"], file.name
        uids.add(data["uid"])
        assert any(v["name"] == "DS_PROMETHEUS" for v in data["templating"]["list"]), "datasource-переменная"
        for panel in data["panels"]:
            pos = panel["gridPos"]
            assert 0 <= pos["x"] and pos["x"] + pos["w"] <= 24 and panel["title"], (file.name, panel.get("title"))
            assert panel["targets"], (file.name, panel["title"])
            for target in panel["targets"]:
                unknown = {t for t in _metric_tokens(target["expr"]) if not _known(t, names)}
                assert not unknown, f"{file.name} / {panel['title']}: неизвестные метрики {unknown}"


def test_helm_chart_carries_identical_copies() -> None:
    assert (HELM / "rules" / "alerts.yaml").read_text() == ALERTS.read_text(), (
        "deploy/helm/parking/rules/alerts.yaml устарел"
    )
    for file in DASHBOARDS.glob("*.json"):
        assert (HELM / "dashboards" / file.name).read_text() == file.read_text(), f"{file.name}: копия в чарте устарела"
    templates = HELM / "templates"
    assert "grafana_dashboard" in (HELM / "values.yaml").read_text()
    assert "PrometheusRule" in (templates / "prometheus-rules.yaml").read_text()
    assert ".Files.Glob" in (templates / "grafana-dashboards.yaml").read_text()
    provisioning = yaml.safe_load((ROOT / "deploy/grafana/provisioning/dashboards/parking.yaml").read_text())
    assert provisioning["providers"][0]["allowUiUpdates"] is False


def test_promtail_label_discipline() -> None:
    config = yaml.safe_load((ROOT / "deploy/loki/promtail-config.yaml").read_text())
    stages = config["scrape_configs"][0]["pipeline_stages"]
    labels = next(s["labels"] for s in stages if "labels" in s)
    assert set(labels) <= {"level", "environment", "component"}, "в лейблы Loki попадают только низкокардинальные поля"
    dropped = next(s["labeldrop"] for s in stages if "labeldrop" in s)
    assert {"tenant_id", "lot_id", "camera_id", "session_id", "correlation_id", "trace_id"} <= set(dropped)
