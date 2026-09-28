# Backend core — фаза 3

**Статус:** 2026-09-19, подфазы 3.1–3.5 закрыты. Gate-выводы — `docs/backend/gate-3.*.out`, бенчмарки — `docs/backend/argon2-bench.out`, `docs/backend/access-bench.out`. Решения: [ADR-10](adr/ADR-10-sql-repositories.md), [ADR-11](adr/ADR-11-events-and-uow.md), [ADR-12](adr/ADR-12-barrier-failure-after-payment.md). Реестр событий — [EVENTS.md](EVENTS.md) (генерируется), контракт API — [openapi.json](openapi.json) (генерируется).

## Структура

```
backend/src/parking/
  shared/        Money, PlateNumber, ошибки, correlation, clock, DomainEvent
  platform/      config (I-14), logging + редактор ПДн (I-10), db (engine, UoW: outbox I-5 + граница HTTP I-9), crypto, redis, events_registry, metrics
  modules/       identity, tenancy, devices, access, tariff, parking, billing, audit — domain / application / infrastructure
  api/           роуты без бизнес-логики, deps (current_user, require), ошибки → HTTP, middleware
  workers/       outbox_publisher, stream_consumer, handlers, celery_app, runtime
  runtime/       composition root (container) — единственное место сборки сервисов из инфраструктуры
```

Границы охраняет `tests/architecture/test_imports.py` (обход AST): domain не видит platform/application/infrastructure и I/O-клиентов; application не видит infrastructure и `platform.http`; чужие модули — только через `application.contracts`; `httpx` — только `platform.http` и `*/infrastructure`; api/workers/runtime не импортируют domain. Тест дважды ловил реальные нарушения в этой фазе.

## Подфазы

| Подфаза | Что сделано | Ключевые тесты |
|---|---|---|
| 3.1 Фундамент | `Money` (без `__float__`, `Fraction`/`Decimal` с явным округлением, `split`/`percent` без потери тийина), `PlateNumber` (нормализация, маска), конфиг с `sys.exit(1)` и prod-валидаторами, structlog-редактор, `UnitOfWork` (outbox в той же транзакции, post-commit хуки, `transaction_depth`), `GuardedTransport`, `PlateCrypto` (HMAC + AES-GCM с AAD tenant‖version) | `tests/shared`, `tests/platform`, `tests/architecture` |
| 3.2 Identity / RBAC / tenancy | argon2id `t=2 m=64MiB p=1` (249 мс на dev-машине, настраивается), JWT EdDSA 15 мин, refresh 7 дней в httpOnly-cookie с ротацией и детекцией reuse (отзыв семейства + аудит + событие), брутфорс: 429 с `Retry-After` по (email, ip) и по ip, lockout после 10 с аудитом, неразличимость ответов и времени (argon2 всегда выполняется); матрица роль → разрешения в коде; `require(Permission)`; скоуп площадок в репозиториях; `x-permission` в OpenAPI → контрактный RBAC-тест генерирует таблицу (endpoint, role) и падает на эндпоинте без разрешения или без sample | `tests/api/test_auth.py`, `test_rbac_contract.py`, `test_tenant_and_scope.py` |
| 3.3 Tariff | чистая `calculate()`: грейс, первый час блоком, поминутный прорейт, шаг и режим округления, дневной максимум по локальному дню, ночное окно через полночь, переопределения по дню недели и праздникам, VIP/абонемент, скидка whitelist без потери тийина; версия по моменту выезда, EXCLUDE → 409; покрытие domain 99 %, hypothesis 300 примеров | `tests/tariff` |
| 3.4 Access | единая таблица правил, `resolve()` одним запросом; кеш Redis (HASH на номер, поле на площадку, TTL ≤ 300 с и не дольше срока правила) + локальный 5 с; инвалидация post-commit + pub/sub + outbox-страховка; деградация без Redis; p95 через Redis 0.45 мс; замер на 1 000 004 строк: единая таблица 78 мс / 1000 запросов против UNION ALL 84 мс | `tests/access` |
| 3.5 Sessions / saga / outbox | ENTRY одной транзакцией (savepoint на I-1, повторный въезд → ORPHAN старой, blacklist и низкая уверенность → MANUAL_REVIEW без команды шлагбауму), EXIT-сага (шаг = транзакция, идемпотентность по `saga_id+step`, компенсации EXPIRED/MANUAL_REVIEW, предоплата, unknown-exit политики), ручные операции с `reason` и аудитом (I-15, I-16), outbox publisher (advisory lock, NOTIFY + 250 мс, SKIP LOCKED, порядок по id, 8 шардов, FAILED после 10), stream consumer (аренда шардов, XAUTOCLAIM, inbox, DLQ), Celery-обвязка, реестр событий с JSON Schema (проверка при `publish` и перед XADD), API сессий/occupancy с курсорной пагинацией, экспорт OpenAPI 3.1 + проверка совместимости | `tests/parking`, `tests/workers`, `tests/api/test_openapi.py` |

## Как запустить локально

```bash
cd backend && uv sync
uv run python scripts/pg_dev.py start && uv run python scripts/redis_dev.py start
uv run alembic upgrade head
uv run python scripts/pg_dev.py psql -d parking_dev -v ON_ERROR_STOP=1 -f scripts/seed_dev.sql && uv run python scripts/seed_dev.py   # печатает dev-пароли, пишет .env.dev
PARKING_ENV=dev uv run uvicorn parking.api.main:app --port 8000                     # API (Swagger: /docs)
PARKING_ENV=dev uv run python -c 'from parking.workers.runtime import main_publisher; main_publisher()'   # outbox → Redis Streams
PARKING_ENV=dev uv run python -c 'from parking.workers.runtime import main_consumer; main_consumer()'     # consumer (inline barrier)
PARKING_ENV=dev uv run celery -A parking.workers.celery_app worker -Q saga,barrier,maintenance            # Celery (prod-режим диспетчеризации)
```

## Что намеренно отложено и как это видно

- **Драйверы шлагбаумов** — сделаны в фазе 4 (`HttpRestDriver`, `ModbusTcpDriver`, см. `DEVICES.md` §3). `PARKING_BARRIER_DRIVERS_ENABLED=false` по-прежнему выключает реестр честно: команда получает `FAILED` с текстом причины, сага — `MANUAL_REVIEW`, не тихий no-op (CORE §7). В тестах ENTRY/EXIT-поток проверяется fake-драйвером из `tests/support/fakes.py`.
- **Подтверждение наличных, смены, провайдеры** — сделаны в фазе 5 (`PAYMENTS.md`): `CashierService`, `OnlinePaymentService`, `WebhookService`, `RefundService`, `ReconciliationService` поверх того же контракта `PaymentObligations`.
- **ANPR Ingest** — сделан в фазе 4 (`modules/anpr`, `DEVICES.md` §1, §4) поверх контракта `ParkingCommands.process_entry_in/start_exit_in` — одна транзакция на событие.
- **Celery в тестах не поднимается**: задачи — тонкие обёртки над `container.parking.advance_saga`, `barrier_executor.execute`, `recover_due`; сама логика покрыта тестами напрямую, включая восстановление после «kill -9» в каждом промежуточном состоянии.
- **oasdiff** недоступен (Go-бинарник с GitHub) — `scripts/openapi_compat.py` проверяет удаление операций/полей, новые обязательные параметры и смену типов; в CI (фаза 9) добавляется oasdiff (A-23).
- **Наблюдаемость** — сделана в фазе 7 (`OBSERVABILITY.md`): сквозной `correlation_id`, OTel-трассы с непрерывностью через outbox/стримы/Celery, метрики с расчётом кардинальности, `/metrics`, Sentry с `before_send`.
- **Vault KeyProvider** — фаза 9; `keys_source=vault` в prod-конфиге обязателен и уже валидируется, реализация появится вместе с инфраструктурой.

## Инварианты, закрытые тестами в фазе 3

I-1 (`test_fifty_parallel_entries_create_one_session`), I-3 на уровне саги/команд (idempotency_key команд и обязательств), I-5 (`test_event_appears_in_stream_only_after_commit`), I-7 (`test_deterministic_non_negative_monotone`), I-8 (`test_blacklist_never_opens_automatically`), I-9 (`test_http_inside_uow_raises_boundary_violation` + AST), I-10 (`test_end_to_end_logger_output`, маска `PlateNumber`), I-12 (`test_new_version_applies_only_from_its_valid_from`), I-13 (`test_money.py`), I-14 (`test_config.py`), I-15 (`test_manual_actions_require_reason`, аудит в каждом ручном действии), I-16 (`test_plate_correction_keeps_original_recognition`).

## Замеры

- argon2id: `t=2 m=64MiB p=1` — 249 мс (Celeron N4020); `t=2 m=32MiB` — 123 мс. Prod-параметры — после бенчмарка на боевом железе (`scripts/bench_argon2.py`).
- Access resolve: p95 0.45 мс через Redis, 0.003 мс из локального кеша; холодный путь — ровно один SELECT (с 2026-09-21 — в транзакции события, `resolve_in`).
- SQL на camera-событие (2026-09-21, `docs/backend/sql-budget.md`): ENTRY 19 → 10, EXIT 16 → 10, одна транзакция; k6 `baseline` на той же песочнице: p95 1652 → 688 мс. Бюджет закреплён тестом `tests/anpr/test_sql_budget.py`.
- 1 000 004 правил: единая таблица 0.078 мс/запрос, UNION ALL по видам 0.084 мс/запрос (Index Scan в обоих случаях).
