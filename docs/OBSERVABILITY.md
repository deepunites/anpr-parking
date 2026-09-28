# Наблюдаемость (фаза 7)

Три сигнала — логи, метрики, трассы — связаны сквозным `correlation_id` (бизнес-запрос) и `trace_id` (OTel).
Gate-артефакты: `docs/backend/gate-7.out`, `docs/backend/trace-exit-saga.json`, `docs/backend/metrics-cardinality.md`.

## 1. Логи

structlog → JSON в stdout (`platform/logging.py`), одна запись — одно событие. Обязательные поля каждой записи:
`timestamp`, `level`, `service`, `environment`, `version`, `correlation_id`, `message` (= `event`); внутри спана —
`trace_id`, `span_id`; где применимо — `tenant_id`, `lot_id`, `camera_id`, `session_id`, `event_type`, `user_id`.
Контекст живёт в `structlog.contextvars` и привязывается:

| Где | Кто привязывает | Что |
|---|---|---|
| HTTP-запрос | `CorrelationMiddleware` (UUID из `X-Correlation-Id` или новый; возвращается тем же заголовком и в теле ошибок) | `correlation_id`; обработчики добавляют `lot_id`/`camera_id`/`session_id` через `bind_log_context` — снимаются по окончании запроса |
| Outbox → Redis Stream | `UnitOfWork` пишет `correlation_id` и `trace_parent` в `outbox`, publisher — в конверт | лог `outbox_event_published` на каждое событие |
| Stream consumer | `correlation_scope(envelope.correlation_id, tenant_id)` + `bind_log_context(lot_id, session_id, camera_id, event_type)` | все записи обработчика и исполнителя шлагбаума |
| Celery | сигналы `before_task_publish` (заголовок `correlation_id`), `task_prerun`/`task_postrun` | `correlation_id`, `celery_task` |
| Исходящие HTTP (провайдеры, драйверы) | OTel-инструментация httpx | `traceparent`; `correlation_id` — в логах вызова |

Тест сквозной корреляции: `tests/observability/test_correlation_end_to_end.py` — одно событие камеры → записи
`anpr_event_accepted` (API), `http.request`, `outbox_event_published` (publisher), `consumer_handled` (consumer),
`barrier_command_completed` (исполнитель) с одним `correlation_id`; вывод — в `gate-7.out`.

**Запрещённое содержимое (I-10)** вырезается процессором `redact_processor` до рендерера: номера (узбекские форматы,
латиница/кириллица, с пробелами и без), JWT, `Bearer …`, длинные ключи (≥40 символов без дефисов), значения ключей
`password|secret|token|authorization|cookie|api_key|x-camera-key|plate|plate_number|card|pan|cvv`. Тест на реальном
JSON-рендерере: `tests/observability/test_sentry_and_redaction.py::test_json_log_output_has_no_forbidden_content`.
`plate_hash`, UUID, `span_id` не задеваются (регрессия `pan` ⊂ `span_id` закрыта тестом).

Loki: лейблы только `app, namespace, component, pod, level, environment` (`deploy/loki/promtail-config.yaml`,
`labeldrop` для `tenant_id/lot_id/camera_id/session_id/correlation_id/trace_id`); поиск по ним — `| json | correlation_id="…"`.
Retention 30 дней; аудит — только PostgreSQL (`audit_log`).

## 2. Метрики

`prometheus_client`, реестр `platform/metrics.py`, `*_created`-ряды отключены. API отдаёт `GET /metrics` (без
аутентификации, не в OpenAPI; перед отдачей `MetricsRefresher` обновляет gauge'и из БД/Redis не чаще раза в 5 с с
таймаутом пробы 3 с — scrape не зависает вместе с зависимостью). Воркеры поднимают `/metrics` через
`PARKING_METRICS_PORT` (`start_http_server`); Celery — `--pool=solo` (один процесс — один реестр).

Полный список с лейблами и расчётом кардинальности — `docs/backend/metrics-cardinality.md` (генерируется тестом
`tests/observability/test_metrics.py`; новый лейбл без оценки — падение теста): **≈ 93 тыс. рядов** при 1000
площадок, 3 репликах API и 2 воркерах — под бюджетом 100 тыс. Дисциплина: без `session_id/correlation_id/user_id/
plate/error`; `lot` допустим, `camera_id` — нигде (здоровье камер агрегируется по площадке `camera_status_by_lot`,
список проблемных — API `GET /api/v1/cameras`); гистограммы с `lot` — минимум бакетов (`camera_clock_skew_seconds`: 3).

SLI (§4 CORE) → метрики: `http_request_duration_seconds{method,route,status_class}` (p95 и доля 5xx по
`/api/v1/camera/events`, доступность API), `exit_end_to_end_seconds{paid}` (событие → шлагбаум открыт, из саги),
`barrier_command_total{command="OPEN",result}` (success rate), `payment_confirm_latency_seconds{method}`.
`revenue_total{method}` — тийины во float64: см. `docs/OPERATIONS.md`.

## 3. Трассировка

OpenTelemetry (`platform/tracing.py`, `platform/telemetry.py`): автоинструментация FastAPI, SQLAlchemy, httpx,
redis, Celery + ручные спаны `entry.process`, `exit.start`, `exit.saga.step` (атрибут `saga.state`),
`tariff.calculate`, `access.resolve`, `barrier.open|close`, `payment.create`, `consume <event_type>`.
Экспорт — OTLP/HTTP (`PARKING_OTEL_EXPORTER_ENDPOINT`), без него спаны только питают `trace_id` в логах.

Непрерывность через очереди: `traceparent` пишется в `outbox.trace_parent` → конверт стрима → consumer
восстанавливает контекст (`context_from_traceparent`) и открывает дочерний спан; Celery — `CeleryInstrumentor`
(заголовки задачи). Тест `tests/observability/test_tracing.py::test_exit_saga_trace_is_continuous_across_stream`:
одна трасса `exit.start` → `consume devices.barrier.command_requested.v1` → `exit.saga.step` → `barrier.open`;
JSON трассы — `docs/backend/trace-exit-saga.json`.

Сэмплинг (`ParkingSampler`, head-based): EXIT-поток (`parking.flow=exit`, `mark_exit_flow()` в ingest) — 100 %;
`/health`, `/metrics`, `/ws` — не пишутся; события камер — `PARKING_OTEL_ENTRY_SAMPLE_RATIO` (0.02);
остальной API — `PARKING_OTEL_API_SAMPLE_RATIO` (0.1); решение родителя наследуется. Несэмплированные спаны
пишутся (`RECORD_ONLY`) и экспортируются `SelectiveExportProcessor`'ом только при ошибке (`StatusCode.ERROR`) или
`parking.force_export` — «ошибки — всегда» без tail-сэмплинга в коллекторе. Исключение (фаза 8, профиль под
нагрузкой): у несэмплированного родителя дочерние спаны БД (`db.system`) и клиентские (`SpanKind.CLIENT`)
отбрасываются (`DROP`), а внутренние спаны ASGI `http send`/`http receive` выключены совсем
(`exclude_spans=["receive","send"]`) — они давали ≈7 % CPU процесса API и не несли информации сверх серверного спана.
Атрибуты `plate|password|token|secret|api_key|authorization|cookie` в спанах запрещены (`ValueError`).

## 4. Дашборды

`deploy/grafana/dashboards/{operations,slo,pipeline,business}.json` — генерируются `deploy/grafana/gen_dashboards.py`
(источник истины, кликанье в UI отключено: `allowUiUpdates: false`). Провижининг: файловый провайдер
`deploy/grafana/provisioning/dashboards/parking.yaml` (VM/compose) и Helm — ConfigMap на каждый JSON с меткой
`grafana_dashboard: "1"` (sidecar kube-prometheus-stack, `deploy/helm/parking/templates/grafana-dashboards.yaml`).
Тест: каждая панель ссылается только на существующие метрики реестра или известных экспортеров.

| Дашборд | Вопрос |
|---|---|
| Operations | Что происходит на площадках прямо сейчас: занятость, въезды/выезды по исходам, шлагбаумы, камеры не ONLINE, оффлайн-режим |
| SLO | Четыре SLI + доступность, error budget за 30 дней, burn rate 1h/6h, латентность по роутам |
| Pipeline | Outbox (pending, возраст, ошибки), стримы по шардам, DLQ, consumer, саги, репликация, Celery, WS |
| Business | Выручка по способам (сутки, в час), платежи по статусам, льготы, ручные коррекции, сверка, возвраты |

## 5. Алерты

`deploy/prometheus/alerts.yaml` (22 правила; в Helm — `PrometheusRule` с подстановкой `runbookBaseUrl`). Каждое
правило: `severity`, `summary`, `description`, `impact`, `runbook_url` → `<a id>` в `docs/RUNBOOK.md`, `for`.
Симптомные — critical (`ExitSuccessRatioLow`, `CameraEvents5xxHigh`, burn-rate), причинные — warning.
Проверки: `promtool check rules`, юнит-тесты порогов и защиты от шума `promtool test rules
deploy/prometheus/alerts_test.yaml` (нет трафика → тишина; 99.5 % OK → тишина; 90 % → алерт; 0.2 % 5xx → тишина),
pytest на полноту аннотаций/якорей/метрик.

Симуляция на живом стенде (`backend/scripts/alert_sim.py`, `for` ÷10, scrape 1 с): остановлен publisher →
`OutboxLag` firing через ≈44 с (порог 30 с + `for` 12 с); остановлен PostgreSQL → `PostgresPrimaryDown` firing
через ≈10 с (`for` 6 с + обнаружение ≤5 с), событие камеры в это время — `202` (оффлайн-буфер); PostgreSQL поднят →
алерт гаснет через ≈4 с без трафика (`MetricsRefresher` пробует БД и закрывает DB-breaker сам). Вывод — `gate-7.out`.
Проверка на шум по данным нагрузочного теста — после фазы 8 (A-44).

## 6. Sentry

Backend: `platform/sentry.py` — `init_sentry` при `PARKING_SENTRY_DSN` (`send_default_pii=False`,
`max_request_body_size="never"`, release `parking@<version>`), `scrub_event` как `before_send`: те же правила
редактирования, заголовки авторизации/cookie → `[REDACTED]`, тело запроса удаляется, `DomainError` → не
отправляется; теги `correlation_id`, `tenant_id`, `user_id` из contextvars. Тест — `test_sentry_and_redaction.py`.

Frontend: `frontend/src/observability/{scrub,sentry}.ts` — SDK грузится отдельным чанком только при
`VITE_SENTRY_DSN`; `beforeSend` = `scrubEvent` (vitest `scrub.test.ts`: номер, JWT, ключ, e-mail, IP не уходят;
4xx/доменные коды не отправляются); release `parking-frontend@<VITE_RELEASE>` (commit sha из CI),
`build.sourcemap="hidden"` — карты загружаются в CI (`sentry-cli sourcemaps upload --release … dist/`) и не раздаются
(nginx: 404 на `*.map`); CSP `connect-src` дополняется origin DSN.

## 7. Локально

```bash
# Prometheus + promtool (официальный релиз, без root)
curl -sSL -o /tmp/p.tgz https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.linux-amd64.tar.gz
mkdir -p ~/.local/prometheus/bin && tar xzf /tmp/p.tgz -C ~/.local/prometheus/bin --strip-components=1 --wildcards '*/prometheus' '*/promtool'
~/.local/prometheus/bin/promtool check rules deploy/prometheus/alerts.yaml
~/.local/prometheus/bin/promtool test rules deploy/prometheus/alerts_test.yaml
# симуляция алертов (нужны pg_dev start, redis_dev start 6390)
cd backend && uv run python scripts/alert_sim.py
# тесты наблюдаемости
cd backend && uv run pytest -q tests/observability -s
```
