"""Матрица §5 фазы 8: каждая строка таблицы → автотест(ы). Проверяется tests/failure/test_00_matrix.py
(все ссылки существуют) и печатается в docs/TESTING.md."""

from __future__ import annotations

MATRIX: list[tuple[str, str, list[str]]] = [
    (
        "PostgreSQL остановлен",
        "API 202, события в Redis Stream, потерь нет",
        [
            "tests/failure/test_01_postgres_outage.py::test_postgres_stopped_then_started_no_loss_no_duplicates",
            "tests/anpr/test_offline.py::test_pg_down_queues_events_and_replay_has_no_duplicates",
        ],
    ),
    (
        "PostgreSQL поднят",
        "все события доехали, дублей нет, счётчики сошлись",
        ["tests/failure/test_01_postgres_outage.py::test_postgres_stopped_then_started_no_loss_no_duplicates"],
    ),
    (
        "Primary упал, standby промотирован",
        "RTO измерен, потери принятых событий нет",
        ["tests/failure/test_09_failover.py::test_primary_crash_promote_standby_measure_rto"],
    ),
    (
        "Redis остановлен",
        "деградация: кеш доступа мимо, оффлайн-буфер недоступен → API 503 честно",
        ["tests/failure/test_03_redis_outage.py::test_redis_stopped_degrades_honestly_and_recovers"],
    ),
    (
        "Redis поднят",
        "восстановление без ручных действий",
        ["tests/failure/test_03_redis_outage.py::test_redis_stopped_degrades_honestly_and_recovers"],
    ),
    (
        "MinIO недоступен",
        "событие обрабатывается, изображение помечено как недоставленное, повтор",
        ["tests/failure/test_04_components.py::test_minio_unavailable_event_processed_frame_deferred_and_retried"],
    ),
    (
        "Шлагбаум не отвечает",
        "3 ретрая, circuit breaker, сессия в нужном состоянии, алерт",
        [
            "tests/failure/test_04_components.py::test_barrier_not_responding_three_retries_breaker_and_manual_review",
            "tests/devices/test_barrier_reliability.py::test_circuit_breaker_opens_after_five_failures_and_probes_after_ttl",
            "tests/failure/test_04_components.py::test_alert_rules_for_failure_scenarios_are_unit_tested[BarrierFailure]",
        ],
    ),
    (
        "Шлагбаум отвечает 500 после оплаты",
        "платёж сохранён, сага в MANUAL_REVIEW",
        [
            "tests/parking/test_exit_saga.py::test_barrier_failure_after_payment_goes_to_manual_review_and_keeps_payment",
            "tests/billing/test_online.py::test_barrier_failure_after_payment_keeps_payment",
        ],
    ),
    (
        "Платёжный провайдер таймаутит",
        "нет повторного создания, запрос статуса, сага ждёт",
        ["tests/billing/test_online.py::test_provider_timeout_does_not_recreate_and_parks_saga_for_verification"],
    ),
    (
        "Провайдер прислал вебхук 5 раз",
        "один эффект",
        [
            "tests/billing/test_webhooks.py::test_payme_full_flow_and_five_identical_performs",
            "tests/billing/test_webhook_race.py::test_concurrent_identical_performs_apply_once",
        ],
    ),
    (
        "Воркер убит kill -9 в середине саги",
        "сага возобновлена, эффект один",
        [
            "tests/parking/test_exit_saga.py::test_saga_resumes_after_crash_in_every_intermediate_state",
            "tests/parking/test_concurrency.py::test_parallel_saga_steps_produce_one_effect_each",
        ],
    ),
    (
        "Outbox publisher убит после публикации, до отметки",
        "потребитель получил дубль, обработал идемпотентно",
        ["tests/workers/test_outbox_and_consumer.py::test_consumer_processes_once_even_when_redelivered"],
    ),
    (
        "Сетевой разрыв между API и БД на 30 с",
        "без потерь, без зависших транзакций",
        ["tests/failure/test_02_network_partition.py::test_thirty_second_partition_no_loss_no_hung_transactions"],
    ),
    (
        "Часы камеры сдвинуты на час",
        "событие принято, помечено, алерт",
        [
            "tests/failure/test_04_components.py::test_camera_clock_one_hour_ahead_is_accepted_and_marked",
            "tests/failure/test_04_components.py::test_alert_rules_for_failure_scenarios_are_unit_tested[CameraClockSkew]",
        ],
    ),
    (
        "Одновременный ENTRY с двух камер по одной машине",
        "одна сессия",
        [
            "tests/failure/test_04_components.py::test_simultaneous_entry_from_two_cameras_one_session",
            "tests/invariants/test_i01_one_active_session.py::test_fifty_concurrent_entries_yield_one_session",
        ],
    ),
    (
        "Дубликат события через 10 минут",
        "тот же ответ, вторая сессия не создана",
        ["tests/failure/test_04_components.py::test_duplicate_after_ten_minutes_same_response_no_second_session"],
    ),
    (
        "Replay всей DLQ",
        "без дублей",
        ["tests/failure/test_04_components.py::test_replay_entire_dlq_without_duplicates"],
    ),
    (
        "Диск PostgreSQL заполнен на 95 %",
        "алерт до отказа записи",
        [
            "tests/failure/test_04_components.py::test_alert_rules_for_failure_scenarios_are_unit_tested[PostgresDiskAlmostFull]"
        ],
    ),
]


def markdown() -> str:
    rows = ["| Сценарий | Ожидаемое поведение | Автотесты |", "|---|---|---|"]
    for scenario, expected, tests in MATRIX:
        rows.append(f"| {scenario} | {expected} | " + "<br>".join(f"`{t}`" for t in tests) + " |")
    return "\n".join(rows)
