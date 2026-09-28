# Эксплуатация (начато в фазе 7; фазы 9–10 дополняют)

## Метрики: кардинальность и бюджет

Расчёт по каждой метрике — `docs/backend/metrics-cardinality.md` (генерируется `tests/observability/test_metrics.py`,
бюджет 100 000 рядов, сейчас ≈ 93 000 при 1000 площадках, 3 репликах API, 2 воркерах и 1 camera-health).
Правила добавления метрики:

1. Лейбл — только из перечня с оценкой (`LABEL_BOUNDS` в тесте); новый лейбл = новая строка в оценке и в этом файле.
2. Никогда: `plate`, `session_id`, `correlation_id`, `user_id`, `camera_id`, текст ошибки, сырой URL (`route` — только шаблон, всё остальное → `<unmatched>`).
3. `lot` — только на счётчиках/gauge'ах и одной гистограмме с 3 бакетами; `lot` × `camera_id` — запрещено.
4. `*_created`-ряды отключены глобально (`disable_created_metrics()`).

Самые дорогие ряды: `exit_total{lot,result}` (21 000), `camera_clock_skew_seconds{lot}` (15 000), `entry_total`
(18 000), `http_request_duration_seconds` (≈ 10 500), `barrier_command_total` (12 000). При > 1000 площадок —
снижать: `result` → классы, `lot` → `tenant` на счётчиках.

## `revenue_total` — не финансовый источник

`revenue_total{method}` — счётчик в тийинах (целое), Prometheus хранит float64: точность целых сохраняется до
2^53 ≈ 9·10^15 тийинов (9·10^13 сум), но счётчик обнуляется при рестарте процесса, суммируется по репликам через
`rate()/increase()` с экстраполяцией и не знает о возвратах/сверке. Это операционный индикатор («выручка идёт»),
а не отчёт: финансовая истина — только PostgreSQL (`payment`, `refund`, `cashier_shift`, ежедневная сверка).
Дашборд Business подписан соответственно.

## Процессы и `/metrics`

| Процесс | Метрики | Как отдаёт |
|---|---|---|
| API (uvicorn, **1 воркер на под**, N реплик; `--timeout-keep-alive` ≥ таймаута простоя ingress/камер, иначе гонка закрытия keep-alive даёт «connection reset» — фаза 8 §7) | HTTP, ingest, платежи/вебхуки, WS, gauge'и из БД/Redis (`MetricsRefresher`) | `GET /metrics` приложения (реестр на процесс: несколько uvicorn-воркеров в одном поде дали бы метрики случайного воркера, A-54) |
| outbox publisher / stream consumer / offline replayer / camera-health | outbox, consumer, саги, шлагбаумы, статусы камер | `PARKING_METRICS_PORT=9464` → `prometheus_client.start_http_server` |
| Celery (`--pool=solo`, очереди saga/barrier/maintenance/payments) | то же | тот же порт; prefork не поддерживается (нет multiprocess-режима) — один процесс на под |

Scrape-конфиг: `job=parking-api` (Service API), `job=parking-worker` (поды воркеров, порт `metrics`);
внешние экспортеры, на которые ссылаются алерты: postgres_exporter (`pg_up`, `pg_replication_lag_seconds`),
node_exporter (`node_filesystem_*`), blackbox (`probe_ssl_earliest_cert_expiry`), pushgateway
(`parking_backup_last_success_timestamp_seconds` из job бэкапа — фаза 9), celery-exporter (`celery_queue_length`, дашборд).

## Алерты, дашборды, runbook

- Правила: `deploy/prometheus/alerts.yaml` (+ `alerts_test.yaml` для `promtool test rules`); Helm — `PrometheusRule`
  (`observability.prometheusRule.labels` под `ruleSelector` оператора), `observability.runbookBaseUrl` подставляется в `runbook_url`.
- Дашборды: `deploy/grafana/dashboards/*.json` ← `deploy/grafana/gen_dashboards.py`; копии в `deploy/helm/parking/dashboards/`
  обязаны совпадать (тест). Grafana sidecar подхватывает ConfigMap с `grafana_dashboard: "1"`, папка `parking`.
- Runbook: `docs/RUNBOOK.md`, якорь на каждый алерт; при добавлении алерта сначала раздел, потом правило (тест падает иначе).

## Логи

Loki/promtail: `deploy/loki/promtail-config.yaml`. Лейблы только низкокардинальные; поля запроса — в JSON строки.
Типовые запросы:

```
{app="parking"} | json | correlation_id="…"                    # вся цепочка запроса/события
{app="parking",component="api"} | json | status >= 500          # 5xx с path (шаблон роута) и duration_ms
{app="parking"} | json | event="consumer_dlq"                    # что уехало в DLQ и почему
{app="parking"} | json | lot_id="…" | event=~"barrier_.*"       # шлагбаумы площадки
```
