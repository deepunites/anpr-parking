# Кардинальность метрик (расчёт, фаза 7 §2)

Оценка сверху при 3 репликах API и 2 воркерах, 1000 площадках. Генерируется тестом `tests/observability/test_metrics.py`; бюджет 100,000 рядов.

| Метрика | Тип | Лейблы | Комбинаций | Рядов/инстанс | Процессы | Инстансов | Рядов |
|---|---|---|---|---|---|---|---|
| `exit_total` | Counter | lot, result | 7,000 | 7,000 | api | 3 | 21,000 |
| `entry_total` | Counter | lot, result | 6,000 | 6,000 | api | 3 | 18,000 |
| `camera_clock_skew_seconds` | Histogram | lot | 1,000 | 5,000 | api | 3 | 15,000 |
| `barrier_command_total` | Counter | lot, command, result | 6,000 | 6,000 | worker | 2 | 12,000 |
| `http_request_duration_seconds` | Histogram | method, route, status_class | 320 | 3,520 | api | 3 | 10,560 |
| `camera_status_by_lot` | Gauge | lot, status | 5,000 | 5,000 | singleton | 1 | 5,000 |
| `session_active` | Gauge | lot | 1,000 | 1,000 | api | 3 | 3,000 |
| `occupancy_ratio` | Gauge | lot | 1,000 | 1,000 | api | 3 | 3,000 |
| `manual_correction_total` | Counter | lot | 1,000 | 1,000 | api | 3 | 3,000 |
| `consumer_processed_total` | Counter | event_type, result | 160 | 160 | worker | 2 | 320 |
| `gateway_seconds` | Histogram | provider | 4 | 36 | api+worker+singleton | 6 | 216 |
| `gateway_calls_total` | Counter | provider, result | 24 | 24 | api+worker+singleton | 6 | 144 |
| `payment_total` | Counter | method, status | 24 | 24 | api+worker+singleton | 6 | 144 |
| `payment_confirm_latency_seconds` | Histogram | method | 4 | 48 | api | 3 | 144 |
| `refund_total` | Counter | method, result | 24 | 24 | api+worker+singleton | 6 | 144 |
| `metrics_refresh_seconds` | Histogram | — | 1 | 17 | api+worker+singleton | 6 | 102 |
| `outbox_published_total` | Counter | event_type | 40 | 40 | worker | 2 | 80 |
| `webhook_total` | Counter | provider, result | 24 | 24 | api | 3 | 72 |
| `exit_end_to_end_seconds` | Histogram | paid | 2 | 28 | worker | 2 | 56 |
| `camera_event_total` | Counter | result | 12 | 12 | api | 3 | 36 |
| `camera_event_rejected_total` | Counter | reason | 12 | 12 | api | 3 | 36 |
| `stream_pending_total` | Gauge | stream | 12 | 12 | api | 3 | 36 |
| `dlq_length` | Gauge | stream | 12 | 12 | api | 3 | 36 |
| `ws_dropped_total` | Counter | type | 12 | 12 | api | 3 | 36 |
| `outbox_batch_seconds` | Histogram | — | 1 | 17 | worker | 2 | 34 |
| `camera_auth_seconds` | Histogram | — | 1 | 11 | api | 3 | 33 |
| `dlq_messages_total` | Counter | stream | 12 | 12 | worker | 2 | 24 |
| `consumer_retry_scheduled_total` | Counter | stream | 12 | 12 | worker | 2 | 24 |
| `redis_stream_lag` | Gauge | stream | 12 | 12 | worker | 2 | 24 |
| `saga_steps_total` | Counter | state | 12 | 12 | worker | 2 | 24 |
| `reconciliation_discrepancies_open` | Gauge | kind | 8 | 8 | api | 3 | 24 |
| `access_decision_total` | Counter | kind | 8 | 8 | api | 3 | 24 |
| `revenue_total` | Counter | method | 4 | 4 | api | 3 | 12 |
| `worker_loop_restarts_total` | Counter | worker | 5 | 5 | worker | 2 | 10 |
| `offline_replayed_total` | Counter | result | 4 | 4 | worker | 2 | 8 |
| `media_store_failed_total` | Counter | — | 1 | 1 | api+worker+singleton | 6 | 6 |
| `media_pending_frames` | Gauge | — | 1 | 1 | api+worker+singleton | 6 | 6 |
| `dependency_up` | Gauge | dependency | 2 | 2 | api | 3 | 6 |
| `outbox_pending_total` | Gauge | — | 1 | 1 | api+worker | 5 | 5 |
| `camera_status_total` | Gauge | status | 5 | 5 | singleton | 1 | 5 |
| `camera_auth_failed_total` | Counter | — | 1 | 1 | api | 3 | 3 |
| `camera_auth_abuse_total` | Counter | — | 1 | 1 | api | 3 | 3 |
| `offline_mode_active` | Gauge | — | 1 | 1 | api | 3 | 3 |
| `offline_queue_length` | Gauge | — | 1 | 1 | api | 3 | 3 |
| `outbox_oldest_pending_age_seconds` | Gauge | — | 1 | 1 | api | 3 | 3 |
| `ws_connections` | Gauge | — | 1 | 1 | api | 3 | 3 |
| `ws_messages_total` | Counter | — | 1 | 1 | api | 3 | 3 |
| `ws_overloaded_total` | Counter | — | 1 | 1 | api | 3 | 3 |
| `webhook_delivery_total` | Counter | result | 3 | 3 | singleton | 1 | 3 |
| `event_journal_written_total` | Counter | — | 1 | 1 | api | 3 | 3 |
| `event_journal_dropped_total` | Counter | — | 1 | 1 | api | 3 | 3 |
| `outbox_failed_total` | Counter | — | 1 | 1 | worker | 2 | 2 |
| `outbox_publish_lag_seconds` | Gauge | — | 1 | 1 | worker | 2 | 2 |
| **Итого** | | | | | | | **92,468** |

Оценки значений лейблов: `lot` ≤ 1000, `event_type` ≤ 40, `reason` ≤ 12, `state` ≤ 12, `command` ≤ 2, `status` ≤ 8, `method` ≤ 4, `provider` ≤ 4, `kind` ≤ 8, `stream` ≤ 12, `worker` ≤ 5, `dependency` ≤ 2, `type` ≤ 12, `status_class` ≤ 4, `paid` ≤ 2; `result` по метрике: `entry` ≤ 6, `exit` ≤ 7, `barrier_command` ≤ 3, `consumer_processed` ≤ 4, `camera_event` ≤ 12, `offline_replayed` ≤ 4, `gateway_calls` ≤ 6, `webhook` ≤ 6, `refund` ≤ 6, `webhook_delivery` ≤ 3; `method×route` (HTTP) ≤ 80.
