# Тестирование — фаза 8

**Статус:** 2026-09-20. Gate-вывод — `docs/backend/gate-8.out`. Артефакты: `docs/backend/coverage-layers.md`,
`docs/backend/mutation-report.md`, `docs/backend/load/*.md`, `security/exceptions.yaml`, `docs/backend/profile-*.md`.
Допущения фазы — `docs/ASSUMPTIONS.md` A-47…A-59 (фаза 9: A-60…A-66).

Принцип фазы: покрытие — индикатор, а не цель; каждый класс тестов ловит свой класс ошибок, и у каждого есть команда
запуска, которую можно выполнить локально без root и Docker (см. `backend/README.md`) и повторить в CI (фаза 9).

| Пакет | Что ловит | Команда (из `backend/`) | Где в CI (фаза 9) |
|---|---|---|---|
| `tests/` (по умолчанию, без `failure`/`e2e`) | юнит + интеграционные на реальных PostgreSQL 16, Redis 7, MinIO | `uv run pytest -q` | job `test` |
| `tests/invariants/` | нарушение I-1…I-16 | `uv run pytest -q tests/invariants` | отдельный job `invariants`, блокирует merge независимо от остальных |
| mutation (`tariff/domain`, `billing/domain`) | тесты, исполняющие код, но ничего не проверяющие | `uv run mutmut run && uv run mutmut results` | nightly |
| `tests/failure/` | поведение при отказах инфраструктуры (§5) | `uv run pytest tests/failure -m failure` | job `failure` (лаборатория primary+standby+toxiproxy) |
| `tests/e2e/` | сквозной путь на поднятом стенде | `uv run pytest tests/e2e -m e2e` | job `e2e` |
| `frontend/e2e` (Playwright) | UI-путь оператора | `npm run e2e` (в `frontend/`) | job `e2e-frontend` |
| k6 (`load/k6/parking.js`) | латентность/потери/утечки под профилем §3 CORE | `uv run python scripts/load_test.py --scenario <name>` | job `load` на staging |
| `tests/security/` + `tests/architecture/` | межтенантный доступ, эскалация, JWT, SSRF, инъекции, RLS, тайминг; границы модулей и SQL-фрагменты | входят в `uv run pytest -q` (`test_timing` — `-m slow`) | job `test` |
| сканеры | уязвимости зависимостей/кода/секретов | `backend/.venv/bin/python scripts/security_scan.py` (из корня) | job `security`, критическая уязвимость блокирует деплой |
| `tests/db/test_pgbouncer_rls.py` (`-m pooler`) | утечка tenant-контекста через PgBouncer transaction pooling | `uv run python ../deploy/postgres/lab/pgha_lab.py pooler-test` (фаза 9) | job `pooler` |
| `deploy/postgres/lab/pgha_lab.py failover/verify/pitr` | failover Patroni, восстановление бэкапа с pg_amcheck и контрольными суммами, PITR (RTO) | из `backend/` (фаза 9, INFRA §3–4) | nightly / verify-ВМ |

## 1. Покрытие по слоям

`uv run pytest -q --cov --cov-report=json` (конфигурация `[tool.coverage.*]` в `pyproject.toml`), затем
`uv run python scripts/coverage_layers.py coverage.json --markdown docs/backend/coverage-layers.md` — скрипт
считает покрытие по слоям таблицы §1 и завершается ненулевым кодом при недоборе.

Важно: SQLAlchemy asyncio исполняет корутины в greenlet'ах — без `concurrency = ["thread", "greenlet"]` coverage
не видит код сервисов после первого `await session.*` (в фазе 8 это занижало `auth_service` до 59 %).

| Слой | Порог | Покрытие | Строк | Статус |
|---|---|---|---|---|
| tariff/domain — деньги | ≥ 95 % | 100.0 % | 232/232 | ✅ |
| billing/domain — деньги | ≥ 95 % | 100.0 % | 263/263 | ✅ |
| parking/domain — сессии, сага | ≥ 90 % | 94.6 % | 229/242 | ✅ |
| access/domain — безопасность | ≥ 90 % | 97.2 % | 70/72 | ✅ |
| прочие */domain | ≥ 85 % | 92.9 % | 536/577 | ✅ |
| */application | ≥ 85 % | 91.9 % | 3554/3866 | ✅ |
| */infrastructure | ≥ 70 % | 87.3 % | 1494/1711 | ✅ |
| api | ≥ 80 % | 94.0 % | 1426/1517 | ✅ |
| platform/shared/runtime/workers | ≥ 70 % | 85.8 % | 1638/1908 | ✅ |
| **проект** | ≥ 80 % | **90.9 %** | | ✅ |

Полный прогон: 464 теста (без `failure`/`e2e`), `coverage.json` → таблица выше; результат — 90.9 % по проекту, все слои выше порогов §1.

Полная таблица по модулям — `docs/backend/coverage-layers.md`.

## 2. Mutation testing

mutmut 3.8 на `modules/tariff/domain` и `modules/billing/domain` с чистыми (без БД) тестами
`tests/tariff/test_engine.py`, `tests/tariff/test_engine_contract.py`, `tests/billing/test_domain.py`,
`tests/shared/test_money.py`. **881 мутант: убито 854, таймаут 4, выжило 23 → 97.4 % (порог 85 %; первый прогон фазы 97.2 %).**
Первый прогон mutmut давал 74.7 % — выжившие показали непроверяемые строки breakdown, snapshot, JSON-контракт правил и
поля расхождений сверки; добавленные тесты закрепили их. Выжившие 23 — обёртки текста сообщений и эквивалентные
мутации, разобраны в `docs/backend/mutation-report.md`. Не запускать параллельно с другими сессиями на
`parking_test`: статистический прогон mutmut пересоздаёт базу.

## 3. Инварианты (`tests/invariants/`, 16 файлов, 72 теста)

Каждый файл начинается с формулировки инварианта, механизма принуждения и последствий нарушения.

| Инвариант | Механизм | Как проверяется |
|---|---|---|
| I-1 одна ACTIVE-сессия на (lot, vehicle) | partial unique index на партиционированной таблице | 50 параллельных ENTRY реальными транзакциями → одна сессия |
| I-2 изоляция tenant | RLS с двумя политиками на каждой tenant-таблице, роли без BYPASSRLS | каталог: у каждой таблицы обе политики; сырой SQL под ролью приложения; вставка/перенос строки в чужой tenant; прямой доступ к партициям |
| I-3 идемпотентность camera-события | advisory lock + уникальный ключ события + кеш ответа | 100 параллельных одинаковых событий → один победитель, одна команда |
| I-4 события не теряются | outbox / Redis Streams / оффлайн-буфер / DLQ | остановка PostgreSQL под потоком: 2xx == в БД + буфер, replay без дублей |
| I-5 публикация после коммита | outbox в той же транзакции, publisher читает только закоммиченное | откат транзакции → в стриме пусто; `after_commit`-хук не срабатывает при ошибке |
| I-6 PAID/REFUNDED неизменяемы | триггер + права ролей | UPDATE суммы/статуса под ролью приложения и владельцем → отказ |
| I-7 тариф детерминирован | чистый домен без I/O, snapshot в саге | AST-проверка отсутствия I/O в `tariff/domain`; hypothesis: одинаковые входы → одинаковый результат; snapshot воспроизводим |
| I-8 blacklist не открывает шлагбаум | правило доступа старше кешей, `decide()` | ENTRY/EXIT blacklist → MANUAL_REVIEW, команда не создаётся; ручное открытие — только с reason и аудитом |
| I-9 в транзакции нет HTTP | `GuardedTransport` + AST-проверка импортов httpx | клиент httpx внутри UoW бросает; `httpx.AsyncClient` создаётся только в `platform/http/client.py` |
| I-10 номер не хранится открыто | HMAC-хеш + шифрование, маска в логах/метриках/трейсах | все колонки/логи/метрики/спаны/Sentry-события: номер не встречается |
| I-11 аудит append-only | GRANT только INSERT/SELECT, триггеры | UPDATE/DELETE под ролями приложения → отказ; ручные действия требуют user_id и reason |
| I-12 версии тарифа без пересечений | EXCLUDE-ограничение по диапазону | пересекающаяся версия → отказ, смежные — разрешены |
| I-13 деньги — целые | `Money` без `__float__`, bigint в БД, integer в OpenAPI | статические проверки схем и DDL |
| I-14 fail-fast конфиг | pydantic-settings без дефолтов для секретов | отсутствие секрета/ключа → процесс не стартует |
| I-15 ручные действия аудируются | `reason` обязателен в API, `_audit()` в каждом сервисе | каждый ручной эндпоинт без reason → 422; с reason → строка audit_log |
| I-16 коррекция не затирает распознавание | `anpr_event.raw_*` неизменяемы, коррекция — отдельная запись | коррекция оператором: исходный номер/уверенность сохранены |

## 4. Интеграционные

Без testcontainers (в среде нет Docker, A-47): реальный PostgreSQL 16 (pgserver, `scripts/pg_dev.py`), Redis 7
(`scripts/redis_dev.py start 6390`, БД 1), MinIO (`scripts/minio_dev.py setup`); фикстуры — `tests/support/`. Никаких
sqlite и мокнутых репозиториев. Обязательный список §3:

- миграции с нуля, откат до base и обратно — `tests/db/test_migrations.py`;
- RLS под ролью приложения — `tests/db/test_roles.py`, `tests/invariants/test_i02_tenant_isolation.py`;
- конкурентные сценарии: параллельные ENTRY (`tests/parking/test_entry.py`, `tests/anpr/test_ingest_api.py`),
  параллельное закрытие сессии несколькими потребителями и оператором (`tests/parking/test_concurrency.py`),
  гонка вебхука провайдера с сохранением платежа и с подтверждением наличных (`tests/billing/test_webhook_race.py`);
- outbox: событие не публикуется при откате — `tests/workers/test_outbox_and_consumer.py`,
  `tests/invariants/test_i05_outbox_after_commit.py`;
- Redis Streams: consumer group, pending/XAUTOCLAIM, отложенные повторы, DLQ, replay —
  `tests/workers/test_outbox_and_consumer.py`, `tests/failure/test_04_components.py::test_replay_entire_dlq_without_duplicates`;
- MinIO: presigned PUT/GET, чужой ключ объекта, lifecycle — `tests/media/test_media.py`.

Найдено этой фазой в коде (исправлено): потеря долей секунды на границе суток в тарифе (`_exact_minutes`),
`MALFORMED` вместо 500 на неполных вебхуках, зависимость `_first_paid_by_session` от порядка реестра, фильтр дат
сверки по TZ сессии БД, отсутствие `barrier`-меток в `barrier_command_total`, 500 при чужом `lot_scope` пользователя.

## 5. Failure-тесты (`tests/failure/`)

Лаборатория `tests/failure/lab.py`: primary + синхронный standby PostgreSQL (initdb/pg_basebackup без root),
Redis 6390, toxiproxy между приложением и primary (`timeout=0` — «чёрная дыра»). `docker stop` заменён остановкой
процессов (`pg_dev`/`redis_dev`/`minio_dev`), `kill -9` воркера — возобновлением саги из каждого промежуточного
состояния и повторной доставкой потребителю. Таблица §5 генерируется из `tests/failure/matrix.py` (тест
`test_00_matrix` проверяет, что каждый указанный тест существует и что эта таблица актуальна):

| Сценарий | Ожидаемое поведение | Автотесты |
|---|---|---|
| PostgreSQL остановлен | API 202, события в Redis Stream, потерь нет | `tests/failure/test_01_postgres_outage.py::test_postgres_stopped_then_started_no_loss_no_duplicates`<br>`tests/anpr/test_offline.py::test_pg_down_queues_events_and_replay_has_no_duplicates` |
| PostgreSQL поднят | все события доехали, дублей нет, счётчики сошлись | `tests/failure/test_01_postgres_outage.py::test_postgres_stopped_then_started_no_loss_no_duplicates` |
| Primary упал, standby промотирован | RTO измерен, потери принятых событий нет | `tests/failure/test_09_failover.py::test_primary_crash_promote_standby_measure_rto` |
| Redis остановлен | деградация: кеш доступа мимо, оффлайн-буфер недоступен → API 503 честно | `tests/failure/test_03_redis_outage.py::test_redis_stopped_degrades_honestly_and_recovers` |
| Redis поднят | восстановление без ручных действий | `tests/failure/test_03_redis_outage.py::test_redis_stopped_degrades_honestly_and_recovers` |
| MinIO недоступен | событие обрабатывается, изображение помечено как недоставленное, повтор | `tests/failure/test_04_components.py::test_minio_unavailable_event_processed_frame_deferred_and_retried` |
| Шлагбаум не отвечает | 3 ретрая, circuit breaker, сессия в нужном состоянии, алерт | `tests/failure/test_04_components.py::test_barrier_not_responding_three_retries_breaker_and_manual_review`<br>`tests/devices/test_barrier_reliability.py::test_circuit_breaker_opens_after_five_failures_and_probes_after_ttl`<br>`tests/failure/test_04_components.py::test_alert_rules_for_failure_scenarios_are_unit_tested[BarrierFailure]` |
| Шлагбаум отвечает 500 после оплаты | платёж сохранён, сага в MANUAL_REVIEW | `tests/parking/test_exit_saga.py::test_barrier_failure_after_payment_goes_to_manual_review_and_keeps_payment`<br>`tests/billing/test_online.py::test_barrier_failure_after_payment_keeps_payment` |
| Платёжный провайдер таймаутит | нет повторного создания, запрос статуса, сага ждёт | `tests/billing/test_online.py::test_provider_timeout_does_not_recreate_and_parks_saga_for_verification` |
| Провайдер прислал вебхук 5 раз | один эффект | `tests/billing/test_webhooks.py::test_payme_full_flow_and_five_identical_performs`<br>`tests/billing/test_webhook_race.py::test_concurrent_identical_performs_apply_once` |
| Воркер убит kill -9 в середине саги | сага возобновлена, эффект один | `tests/parking/test_exit_saga.py::test_saga_resumes_after_crash_in_every_intermediate_state`<br>`tests/parking/test_concurrency.py::test_parallel_saga_steps_produce_one_effect_each` |
| Outbox publisher убит после публикации, до отметки | потребитель получил дубль, обработал идемпотентно | `tests/workers/test_outbox_and_consumer.py::test_consumer_processes_once_even_when_redelivered` |
| Сетевой разрыв между API и БД на 30 с | без потерь, без зависших транзакций | `tests/failure/test_02_network_partition.py::test_thirty_second_partition_no_loss_no_hung_transactions` |
| Часы камеры сдвинуты на час | событие принято, помечено, алерт | `tests/failure/test_04_components.py::test_camera_clock_one_hour_ahead_is_accepted_and_marked`<br>`tests/failure/test_04_components.py::test_alert_rules_for_failure_scenarios_are_unit_tested[CameraClockSkew]` |
| Одновременный ENTRY с двух камер по одной машине | одна сессия | `tests/failure/test_04_components.py::test_simultaneous_entry_from_two_cameras_one_session`<br>`tests/invariants/test_i01_one_active_session.py::test_fifty_concurrent_entries_yield_one_session` |
| Дубликат события через 10 минут | тот же ответ, вторая сессия не создана | `tests/failure/test_04_components.py::test_duplicate_after_ten_minutes_same_response_no_second_session` |
| Replay всей DLQ | без дублей | `tests/failure/test_04_components.py::test_replay_entire_dlq_without_duplicates` |
| Диск PostgreSQL заполнен на 95 % | алерт до отказа записи | `tests/failure/test_04_components.py::test_alert_rules_for_failure_scenarios_are_unit_tested[PostgresDiskAlmostFull]` |

Что изменилось в коде по итогам: `command_timeout` по ролям пула и `terminate()` соединения при инвалидации (раньше
graceful close asyncpg висел 60 с при разрыве), breaker БД открывается сразу по таймауту (`trip()`), fake-драйверы
шлагбаума в лаборатории, отложенные кадры media при недоступном MinIO (`media:pending`, Celery retry), метки
`barrier_command_total`, replay DLQ только для PLATFORM_ADMIN.

## 6. E2E

`backend/scripts/e2e_stack.py` поднимает отдельную БД `parking_e2e`, Redis (БД 2, очищается при старте), API
(uvicorn, `PARKING_E2E_WORKERS`), publisher, consumer, replayer, фейковый контроллер шлагбаума
(`scripts/fake_barrier.py`, HTTP_REST, площадка A2 с `barrier_cidr_allowlist`). `tests/e2e/test_full_path.py`
(`-m e2e`, 8 тестов):

- полный путь: ENTRY → сессия → шлагбаум открыт контроллером → EXIT → расчёт (snapshot в саге) → наличные кассиром →
  сага COMPLETED → второе открытие → CLOSED → outbox PUBLISHED → сессия и платёж видны в API;
- варианты: абонемент и VIP (без оплаты), whitelist со скидкой 50 %, blacklist (шлагбаум не открылся, MANUAL_REVIEW),
  низкая уверенность → очередь → подтверждение оператором, ручная коррекция номера → EXIT сходится по новому номеру,
  осиротевшая сессия помечена и видна в списке.

Frontend E2E (Playwright, фаза 6) — `npm run e2e` в `frontend/`, поднимает тот же стенд.

## 7. Нагрузочные (k6)

`load/k6/parking.js`: профиль §3 CORE — ENTRY:EXIT ≈ 1:1 (EXIT — по своей очереди въехавших VU), дубликаты ≈ 5 %,
низкая уверенность ≈ 3 %, ≈ 30 % въездов — льготники (`VIP_PLATES`, выезд без оплаты → SLI EXIT end-to-end),
heartbeat 100 RPS параллельно, операторы — 50 WebSocket + запросы API. Ключи многих камер (`--cameras`) — лимит
120/мин на ключ. Сценарии: `baseline` (30 RPS, 1 ч), `peak` (200 RPS, 30 мин), `absolute` (400 RPS, 5 мин),
`soak` (30 RPS, 12 ч), `operators`; heartbeat — во всех. `scripts/load_test.py` поднимает стенд, создаёт ключи и
VIP-правила, снимает каждые 5 с `/metrics` API и воркеров, `pg_stat_activity`, WAL, лаг реплики (`--lab`), Redis PING,
RSS процессов; после прогона сверяет `принято − дубли == в БД (по recognition.version = RUN_ID) + DLQ + оффлайн-буфер`
и пишет `docs/backend/load/<scenario>.md|json`.

Стенд фазы 8 — песочница 2 vCPU / 3.4 ГБ без Docker, на которой одновременно живут k6, PostgreSQL, Redis, 2 воркера
API, consumer, publisher, replayer и sampler. Поэтому прогоны выполнены с делителями `--scale` (длительность) и
`--rate-scale` (интенсивность), а критерии латентности **не ослаблены** (A-49): их проверка на репрезентативном
железе — staging фазы 9 (`SCALE=1 RATE_SCALE=1`, soak 12 ч).

| Сценарий (§6) | Прогон на песочнице | p95 / p99 POST /camera/events | error | EXIT e2e p95 (n) | дубли сессий | потери (принято → БД+DLQ+буфер) | рост RSS max | outbox_pending | лаг реплики | лаг стримов max | итог |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline (30 RPS · 1 ч) | 6 соб./с, 139 с, 2 воркера API, ÷30 длит., ÷5 интенс. | 1652 / 2535 ms ❌ | 0.000 % ✅ | 9.62 с (313) ❌ | 0 ✅ | 0 (721 − 42 дублей → 679 + 0 + 0) ✅ | 4.0 % ✅ | не растёт ✅ | n/a ✅ | 52 | 6/9 |
| peak (200 RPS · 30 мин) | 20 соб./с, 79 с, 2 воркера API, ÷30 длит., ÷10 интенс. | 9475 / 11385 ms ❌ | 0.000 % ✅ | 113.42 с (95) ❌ | 0 ✅ | 0 (849 − 38 дублей → 811 + 0 + 0) ✅ | 5.6 % ✅ | не растёт ✅ | 0.01 с ✅ | 672 | 6/9 |
| absolute (400 RPS · 5 мин) | 40 соб./с, 84 с, 2 воркера API, ÷5 длит., ÷10 интенс. | 20492 / 28592 ms ❌ | 0.194 % ❌ | 115.50 с (99) ❌ | 0 ✅ | 0 (1028 − 52 дублей → 976 + 0 + 0) ✅ | 8.4 % ✅ | не растёт ✅ | n/a ✅ | 819 | 5/9 |
| operators (50 WS + API · 10 мин) | 2 соб./с, 85 с, 2 воркера API, ÷10 длит., ÷5 интенс. | 418 / 1069 ms ❌ | 0.000 % ✅ | 0.72 с (45) ✅ | 0 ✅ | 0 (121 − 3 дублей → 118 + 0 + 0) ✅ | 1.9 % ✅ | не растёт ✅ | n/a ✅ | 1 | 7/9 |
| soak (30 RPS · 12 ч) | 6 соб./с, 325 с, 2 воркера API, ÷144 длит., ÷5 интенс. | 3022 / 4882 ms ❌ | 0.000 % ✅ | 27.89 с (756) ❌ | 0 ✅ | 0 (1791 − 85 дублей → 1706 + 0 + 0) ✅ | 3.1 % ✅ | не растёт ✅ | n/a ✅ | 157 | 6/9 |

Как читать таблицу: потери, дубли сессий, память, outbox и лаг реплики — критерии, не зависящие от мощности стенда,
и они выполнены во всех сценариях; латентность и EXIT end-to-end на 2 vCPU определяются очередью за CPU
(при 20–40 соб./с стенд насыщен: лаг стримов растёт, `absolute` — предел). `operators` (2 соб./с + 10 WS) — единственный
сценарий с запасом CPU: EXIT end-to-end p95 0.72 с, heartbeat p95 81 мс — так ведёт себя конвейер без насыщения.

Что нашли и починили под нагрузкой: (1) `StreamConsumer._requeue` спал 1/4/16 с прямо в цикле — один падающий
обработчик останавливал все шарды процесса; теперь повторы уходят в ZSET `events:{n}:delayed` и возвращаются в стрим
по сроку; (2) шарды обрабатывались строго последовательно — задержка ≈ 10–20 с на каждый шаг саги при 6 событиях/с;
теперь шарды обрабатываются параллельно (порядок внутри шарда сохранён), EXIT end-to-end p95 при 6 соб./с упал с 59 с
до 9.6 с (остаток — очередь за CPU); (3) метрика `exit_end_to_end_seconds` наблюдается в consumer'е — стенд и sampler
теперь читают `/metrics` воркеров; (4) `redis_stream_lag` и `stream_pending_total` считали только не подтверждённые
записи (остановившийся consumer давал 0) — теперь `XINFO GROUPS lag + pending`; (5) стенд очищает свою БД Redis при
старте (иначе события прошлого прогона → NotFoundError → DLQ); (6) `soak`: 469 саг ушли в MANUAL_REVIEW с «barrier
command PENDING» — команда стояла в очереди дольше дедлайна шага (30 с), затем исполнялась, шлагбаум открывался, а
сессия оставалась у оператора; теперь опоздавший `OK` закрывает сессию как выезд по камере (`barrier_result=OK_LATE`,
`tests/parking/test_exit_saga.py::test_late_barrier_success_after_review_closes_session_as_camera_exit`);
(7) ошибки `absolute`/`soak` (0.19 % / 0.45 %) — не 5xx, а `connection reset` на keep-alive: uvicorn закрывал простаивавшее
соединение (5 с) в момент, когда k6 его переиспользовал; стенд запускает uvicorn с `--timeout-keep-alive 75`, для
production — требование к ingress/таймаутам в OPERATIONS. Отчёты `baseline`/`peak`/`absolute`/`operators` сняты до правок (6) и (7); `soak` в таблице — повторный
прогон после них.

Повторный `soak` (÷144 длительности, 325 с, 6 соб./с) после правок: ошибок 0 (было 8 сбросов соединения), саг в
MANUAL_REVIEW 0 (было 469), потерь 0, дублей 0, RSS API −2.7 % / consumer +3.1 %, outbox не растёт; все 756 выездов
завершены, EXIT end-to-end p95 27.9 с — хвост очереди consumer'а, который раньше прятался в MANUAL_REVIEW. RSS
за 12 часов на этом стенде не измерялся (A-49).

Профиль API до/после — `docs/backend/profile-before.md`, `docs/backend/profile-after.md`
(`PARKING_E2E_PROFILE=1`, py-spy `--nonblocking`, сводка `scripts/profile_summary.py`).

## 8. Безопасность

Сканеры одной командой `scripts/security_scan.py` (bandit с `security/bandit.yaml`, pip-audit `--strict`, npm audit,
trivy fs, gitleaks `dir` с `security/gitleaks.toml`, semgrep p/python, p/fastapi, p/react, p/typescript). Принятые риски —
`security/exceptions.yaml` (дата принятия, `review_by`, владелец; исключение с истёкшим сроком роняет скан). Trivy для
образа — после появления Dockerfile (фаза 9), gitleaks по всей истории — в CI на репозитории (здесь его нет, A-50).

Ручные проверки §7 как тесты:

| Проверка | Тест |
|---|---|
| межтенантный доступ по каждому эндпоинту с ресурсом | `tests/security/test_cross_tenant_matrix.py` — матрица из OpenAPI: каждый путь с `{id}` вызывается токеном чужого tenant с реальным ресурсом (правило, обязательство, ключ, смена, план, сверка) → 404/403, владелец → 200; тест падает, если появился эндпоинт вне матрицы |
| эскалация через `tenant_id`/`role` в теле и в JWT | `test_hardening.py::test_tenant_id_and_role_in_body_do_not_escalate`, `::test_forged_jwt_variants_are_rejected` |
| JWT `alg: none` и подменённый алгоритм | `::test_forged_jwt_variants_are_rejected` (none, HS256 с публичным ключом как секретом, чужой ключ, истёкший) |
| camera-событие без ключа / с чужим ключом | `::test_camera_events_without_key_or_with_foreign_key_are_rejected` |
| SSRF через endpoint шлагбаума и `image_object_key` | `::test_barrier_endpoint_ssrf_is_rejected`, `::test_image_object_key_cannot_point_outside_camera_namespace`, `tests/devices/test_endpoint_policy.py` |
| SQL-инъекции во всех поисковых параметрах, включая номер | `::test_query_parameters_are_injection_safe` (все query-параметры из OpenAPI), `::test_plate_search_and_bodies_are_injection_safe`; статически — `tests/architecture/test_sql_fragments.py` |
| обход RLS прямым SQL под ролью приложения | `::test_app_role_cannot_disable_or_bypass_rls`, `tests/invariants/test_i02_tenant_isolation.py` |
| тайминговая атака на API-ключ | `tests/security/test_timing.py` (`-m slow`): 120 замеров на класс (верный префикс с ошибкой в первом байте секрета / в последнем байте / неизвестный префикс), медианы в пределах шума — сравнение `hmac.compare_digest`, фиктивный digest при отсутствии кандидатов |

## 9. Добавлено в фазе 10

| Что | Тест | Как запускать |
|---|---|---|
| Процесс стирания ПДн: полный цикл REQUESTED→COMPLETED, необратимость (БД, `pg_dump`, логи, кеши, MinIO-версии), финансы без изменений; активная сессия блокирует | `tests/privacy/test_erasure.py` (GATE-строка) | в наборе по умолчанию (нужны PostgreSQL, Redis, MinIO) |
| `VaultKeyProvider`: AppRole login, KV v2 (`tenant_keys_json` и прямой map), `refresh()` считает ротации | `tests/platform/test_vault_keys.py` (`httpx.MockTransport`) | по умолчанию |
| Redis Sentinel: разбор `redis+sentinel://`, брокер Celery; учение — master SIGKILL, тот же клиент пишет снова (RTO 2.5 с) | `tests/failure/test_redis_sentinel.py` | `uv run pytest tests/failure/test_redis_sentinel.py -m failure -s` (локальные `.redis/bin`) |
| Супервизор циклов воркеров: рестарт после инфраструктурной ошибки, метрика `worker_loop_restarts_total` | `tests/workers/test_shutdown.py::test_supervisor_restarts_loop_after_infrastructure_error` | по умолчанию |
| Сбой зависимости → 503 `DEPENDENCY_UNAVAILABLE` + `Retry-After`, а не 500 | `tests/api/test_health.py::test_dependency_outage_is_503_not_500` | по умолчанию |
| RBAC и межтенантная матрица для `/api/v1/privacy/*` | `tests/api/test_rbac_contract.py`, `tests/security/test_cross_tenant_matrix.py` (ресурс `request_id`) | по умолчанию |
| Учения RUNBOOK (не pytest): PG failover через Patroni+PgBouncer, отказ Redis, рост DLQ с replay | `docs/backend/runbook-drills/drill_*.sh`, логи в `docs/RUNBOOK.md` часть C | вручную, нужна лаборатория `deploy/postgres/lab` и стенд `scripts/e2e_stack.py` |

