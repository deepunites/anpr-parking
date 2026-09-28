"""Prometheus-метрики платформы. Лейблы низкой кардинальности (фаза 7): никаких id, номеров, correlation."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, disable_created_metrics

registry = CollectorRegistry()
disable_created_metrics()  # type: ignore[no-untyped-call]  # без *_created-рядов: −1 ряд на каждый счётчик/гистограмму (кардинальность §2)

outbox_published_total = Counter(
    "outbox_published_total", "Событий опубликовано в Redis Streams", ["event_type"], registry=registry
)
outbox_failed_total = Counter(
    "outbox_failed_total", "Событий переведено в FAILED после исчерпания попыток", registry=registry
)
outbox_pending_total = Gauge("outbox_pending_total", "Событий в статусе PENDING", registry=registry)
outbox_publish_lag_seconds = Gauge(
    "outbox_publish_lag_seconds", "Возраст самого старого PENDING-события", registry=registry
)
outbox_batch_seconds = Histogram("outbox_batch_seconds", "Длительность пакета публикации", registry=registry)

consumer_processed_total = Counter(
    "consumer_processed_total", "Обработано событий", ["event_type", "result"], registry=registry
)
consumer_dlq_total = Counter("dlq_messages_total", "Событий отправлено в DLQ", ["stream"], registry=registry)
worker_loop_restarts_total = Counter(
    "worker_loop_restarts_total",
    "Перезапусков цикла воркера супервизором после инфраструктурной ошибки",
    ["worker"],
    registry=registry,
)
consumer_retry_scheduled_total = Counter(
    "consumer_retry_scheduled_total", "Событий поставлено на отложенный повтор", ["stream"], registry=registry
)
redis_stream_lag = Gauge(
    "redis_stream_lag",
    "Отставание consumer group: не доставленные + не подтверждённые записи",
    ["stream"],
    registry=registry,
)

saga_steps_total = Counter("saga_steps_total", "Шагов саги выполнено", ["state"], registry=registry)
barrier_command_total = Counter(
    "barrier_command_total", "Команды шлагбаумам", ["lot", "command", "result"], registry=registry
)

camera_event_total = Counter("camera_event_total", "Принято событий камер", ["result"], registry=registry)
camera_event_rejected_total = Counter(
    "camera_event_rejected_total", "Отклонено событий камер", ["reason"], registry=registry
)
camera_clock_skew_seconds = Histogram(
    "camera_clock_skew_seconds",
    "|event_time − received_at| по площадкам",
    ["lot"],
    buckets=(60, 600),  # 3 бакета: с лейблом lot гистограмма — самый дорогой ряд (расчёт §2)
    registry=registry,
)
camera_auth_failed_total = Counter("camera_auth_failed_total", "Неудачных аутентификаций камер", registry=registry)
camera_auth_abuse_total = Counter(
    "camera_auth_abuse_total", "Превышений порога неудачных аутентификаций с одного адреса", registry=registry
)
camera_auth_seconds = Histogram(
    "camera_auth_seconds",
    "Длительность аутентификации камеры",
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1),
    registry=registry,
)
offline_mode_active = Gauge(
    "offline_mode_active", "1 — PostgreSQL недоступен, приём идёт в Redis Stream", registry=registry
)
offline_queue_length = Gauge("offline_queue_length", "Записей в стриме anpr:offline", registry=registry)
offline_replayed_total = Counter(
    "offline_replayed_total", "Событий воспроизведено из оффлайн-буфера", ["result"], registry=registry
)
camera_status_total = Gauge("camera_status_total", "Камер в статусе", ["status"], registry=registry)
gateway_calls_total = Counter(
    "gateway_calls_total", "Вызовы платёжных провайдеров", ["provider", "result"], registry=registry
)
gateway_seconds = Histogram(
    "gateway_seconds",
    "Длительность вызова провайдера",
    ["provider"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5),
    registry=registry,
)
webhook_total = Counter("webhook_total", "Вебхуки провайдеров", ["provider", "result"], registry=registry)
payment_total = Counter("payment_total", "Платежи по способу и статусу", ["method", "status"], registry=registry)
payment_confirm_latency_seconds = Histogram(
    "payment_confirm_latency_seconds",
    "От создания обязательства до PAID",
    ["method"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 900, 1800),
    registry=registry,
)
revenue_total = Counter(
    "revenue_total",
    "Выручка в тийинах (float64 Prometheus: точен до 2^53 — не финансовый источник, истина в PostgreSQL)",
    ["method"],
    registry=registry,
)
refund_total = Counter("refund_total", "Возвраты", ["method", "result"], registry=registry)
reconciliation_discrepancies_open = Gauge(
    "reconciliation_discrepancies_open", "Открытых расхождений сверки", ["kind"], registry=registry
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Латентность HTTP по шаблону роута (не сырому URL) и классу статуса — SLI §4 CORE",
    ["method", "route", "status_class"],
    buckets=(0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1, 2.5),  # пороги SLO 150/250 мс — границы бакетов
    registry=registry,
)
exit_end_to_end_seconds = Histogram(
    "exit_end_to_end_seconds",
    "EXIT end-to-end: событие камеры → шлагбаум открыт (SLI: p95 < 2 s без оплаты)",
    ["paid"],
    buckets=(0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 10, 30, 120),
    registry=registry,
)
media_store_failed_total = Counter(
    "media_store_failed_total", "Кадров не удалось положить в хранилище сразу (отложены на повтор)", registry=registry
)
media_pending_frames = Gauge("media_pending_frames", "Кадров в очереди повторной отправки", registry=registry)
entry_total = Counter("entry_total", "ENTRY-события по исходу", ["lot", "result"], registry=registry)
exit_total = Counter("exit_total", "EXIT-события по исходу", ["lot", "result"], registry=registry)
session_active = Gauge("session_active", "Активных сессий (занято мест)", ["lot"], registry=registry)
occupancy_ratio = Gauge("occupancy_ratio", "Занятость 0..1+", ["lot"], registry=registry)
access_decision_total = Counter("access_decision_total", "Решения доступа по виду", ["kind"], registry=registry)
manual_correction_total = Counter("manual_correction_total", "Ручные коррекции номера", ["lot"], registry=registry)
camera_status_by_lot = Gauge(
    "camera_status_by_lot", "Камер по статусу на площадке", ["lot", "status"], registry=registry
)
dependency_up = Gauge("dependency_up", "1 — зависимость отвечает (postgres/redis)", ["dependency"], registry=registry)
outbox_oldest_pending_age_seconds = Gauge(
    "outbox_oldest_pending_age_seconds", "Возраст самого старого PENDING в outbox (со стороны API)", registry=registry
)
stream_pending_total = Gauge(
    "stream_pending_total", "Pending-записей consumer group по стримам", ["stream"], registry=registry
)
dlq_length = Gauge("dlq_length", "Записей в DLQ-стримах", ["stream"], registry=registry)
metrics_refresh_seconds = Histogram(
    "metrics_refresh_seconds", "Длительность обновления gauge-метрик на scrape", registry=registry
)
ws_connections = Gauge("ws_connections", "Открытых операторских WebSocket-соединений", registry=registry)
ws_messages_total = Counter("ws_messages_total", "Сообщений отправлено в WebSocket", registry=registry)
ws_dropped_total = Counter("ws_dropped_total", "Сообщений дропнуто бэкпрешером (с GAP)", ["type"], registry=registry)
ws_overloaded_total = Counter("ws_overloaded_total", "Соединений закрыто 1013 из-за переполнения", registry=registry)
# v2 этапы 6–8: интеграции
webhook_delivery_total = Counter(
    "webhook_delivery_total", "Попыток доставки исходящих вебхуков по итогу", ["result"], registry=registry
)
event_journal_written_total = Counter(
    "event_journal_written_total", "Строк журнала событий записано", registry=registry
)
event_journal_dropped_total = Counter(
    "event_journal_dropped_total", "Строк журнала событий потеряно (буфер полон или БД недоступна)", registry=registry
)
