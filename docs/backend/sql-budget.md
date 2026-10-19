# Стоимость camera-события: SQL-бюджет (долг §4 п.1 PRODUCTION_READINESS) — 2026-09-21

Профиль фазы 8 назвал следующим кандидатом на оптимизацию ≈ 18 SQL-операторов на ENTRY (`profile-after.md`).
Замер зондом (слушатель `before_cursor_execute` на движках пула, `tests/anpr/test_sql_budget.py`) на холодном
прогоне дал **ENTRY 19, EXIT 16** операторов; ниже — что из них было лишним и что осталось.

## Было → стало (установившийся режим: ключ камеры в Redis, справочники прогреты)

| Событие | Было | Стало | Транзакций PostgreSQL |
|---|---|---|---|
| ENTRY | 19 (10 SELECT, 7 INSERT, SAVEPOINT + RELEASE) | **10** (4 SELECT, 6 INSERT) | 2 → **1** |
| EXIT | 16 (12 SELECT, 4 INSERT) | **10** (6 SELECT, 4 INSERT) | 1 → 1 |

Что осталось на ENTRY: `set_config` (RLS), advisory-lock, чтение реестра идемпотентности, upsert `vehicle`,
`parking_session`, `barrier_command`, `anpr_event`, запись реестра, один `INSERT INTO outbox` на все события UoW;
плюс (только для номера без решения в кешах) чтение правил доступа — в той же транзакции. С 2026-10-15 ENTRY — 11:
проверка «эха выезда» (стрела одна на въезд и выезд, камера въезда видит выезжающую машину) — одна выборка по
`ix_session_vehicle`, бюджет теста `tests/anpr/test_sql_budget.py` поднят до 11.
На EXIT: `set_config`, lock, реестр, сессия `FOR UPDATE`, активная сага, предоплата, `exit_saga`, `anpr_event`, реестр, outbox.
С 2026-10-19 EXIT обычной машины — 11: въезды этой машины за 24 часа (бесплатные минуты только первым въездам —
тариф `free_visits`), одна выборка по `ix_session_vehicle`; VIP, абонемент и белый список её не делают.

## Что изменено

| # | Было | Стало | Файлы |
|---|---|---|---|
| 1 | Площадка читалась из `parking_lot` на каждое событие; на EXIT — дважды (сервис парковки и калькулятор тарифа) | Кеш процесса `(tenant_id, lot_id) → LotInfo`, TTL `reference_cache_ttl_seconds` (10 с); запись через `SqlLotRepository` сбрасывает запись своего процесса сразу | `shared/ttl_cache.py`, `tenancy/infrastructure/repositories.py` |
| 2 | Шлагбаум камеры (`barrier ⋈ parking_lot`) — на каждый ENTRY | Кеш процесса `(tenant_id, lot_id, camera_id)`; «нет шлагбаума» тоже кешируется на TTL; `add/save` шлагбаума сбрасывают записи площадки | `devices/infrastructure/repositories.py` |
| 3 | Версия тарифа + отдельный `SELECT day FROM holiday` на каждый EXIT | Один SQL (праздники агрегируются `array_agg` в подзапросе) и кеш процесса по площадке; запись годится, пока `valid_from ≤ at < valid_to`; публикация версии/плана сбрасывает кеш tenant'а | `tariff/application/ports.py` (`VersionRecord.valid_from/valid_to/holidays`), `tariff/infrastructure/repositories.py`, `tariff/application/service.py` |
| 4 | `parking_session` вставлялась внутри `SAVEPOINT`/`RELEASE`, чтобы конфликт I-1 не убивал транзакцию | `INSERT … ON CONFLICT (lot_id, vehicle_id) WHERE status = 'ACTIVE' DO NOTHING RETURNING id` — арбитр частичного индекса `uq_session_one_active` (работает на hash-партициях: индекс включает ключ партиционирования); пустой RETURNING → `ConflictError`, транзакция жива | `parking/infrastructure/repositories.py` |
| 5 | Проверка реестра идемпотентности до блокировки и ещё раз после неё | Сразу advisory-lock, одна проверка: дубли после коммита оригинала ловит Redis-кеш ответов (`_cached_response`), а предварительная проверка стоила лишний SQL каждому новому событию | `anpr/application/ingest.py` |
| 6 | Решение доступа — отдельная короткая транзакция до основной (второе соединение, BEGIN + `set_config` + SELECT + COMMIT) | `CachedAccessResolver.resolve_in(uow, …)`: кеши те же (локальный 5 с → Redis), промах читает правила в транзакции события; дубль возвращается до чтения правил | `access/application/resolver.py`, `anpr/application/ports.py`, `ingest.py` |
| 7 | `request_open` искал команду по `idempotency_key`, хотя ключ ENTRY — `sha256("entry", session.id)` только что созданной сессии | `fresh_key=True` пропускает поиск (контракт `BarrierCommands` расширен, для саги поведение прежнее) | `devices/application/{contracts,commands}.py`, `parking/application/services.py` |
| 8 | `INSERT INTO outbox` на каждое интеграционное событие UoW (ENTRY публикует 2–3) | Одна `executemany`-вставка: asyncpg отправляет все строки одним конвейером | `platform/db/uow.py` |

Инварианты не тронуты: I-1 по-прежнему держит уникальный индекс; идемпотентность — advisory-lock + реестр в той же
транзакции; RLS — ключи всех кешей начинаются с `tenant_id`, чтение чужой площадки из кеша невозможно
(`test_reference_caches_do_not_cross_tenants`); граница I-9 не изменилась (внутри транзакции по-прежнему только Redis).

## Семантика кешей процесса

- TTL один на три справочника — `PARKING_REFERENCE_CACHE_TTL_SECONDS` (Helm `config.referenceCacheTtlSeconds`, по умолчанию
  10 с, диапазон 0–300; `0` выключает). Изменение площадки/шлагбаума/тарифа вступает в силу: в поде, через который оно
  сделано, — немедленно (сброс в репозитории до коммита; окно гонки ограничено тем же TTL), в остальных подах — не позже
  TTL. Это та же модель, что у локального кеша решений доступа (ADR-04, N = 5 с); кросс-подовая инвалидация через
  `tariff.version.published.v1`/`tenancy.lot.updated.v1` при желании добавляется подписчиком, не меняя код чтения.
- «Нет площадки» не кешируется (после создания она видна сразу), «нет шлагбаума» — кешируется (иначе площадка без
  шлагбаума ходила бы в БД дважды на каждый въезд).
- Размер ограничен (`max_items` 10 000, при переполнении — полный сброс); TTL и размер — вопрос конфигурации, не памяти:
  запись LotInfo/BarrierInfo/VersionRecord — сотни байт.

## Проверка

- `tests/anpr/test_sql_budget.py` — бюджет 10/10 операторов на прогретом стенде, отсутствие чтений `parking_lot`/`barrier`/
  `tariff_plan`/`holiday` на горячем пути, ровно один `set_config` и один `INSERT INTO outbox` на событие; изоляция кеша по
  tenant'у; немедленная видимость правки площадки в своём процессе. `tests/shared/test_ttl_cache.py` — TTL, `0`, сброс,
  переполнение.
- Полный прогон: `482 passed, 2 skipped` (как gate-10) + новые 7; e2e на поднятом стенде (`tests/e2e -m e2e`): 8 passed;
  `ruff`, `ruff format`, `mypy --strict` — чисто; Helm: lint/template/kubeconform для трёх наборов values.
- k6 `baseline` на той же песочнице (2 vCPU, SCALE 1/30, RATE_SCALE 1/5, 2 воркера, 721 принятое событие в обоих прогонах),
  одиночный прогон каждый; отчёт `load/baseline-sql-budget.md`, фаза 8 — `load/baseline.md`:

| Метрика | Фаза 8 | После | Критерий ТЗ |
|---|---|---|---|
| POST /camera/events p95 | 1651.8 мс | **688.2 мс** (−58 %) | < 150 мс — на песочнице по-прежнему нет |
| POST /camera/events p99 | 2535.3 мс | **1174.4 мс** (−54 %) | < 400 мс |
| EXIT end-to-end p95 | 9.62 с | **4.29 с** (−55 %) | < 2 с |
| heartbeat p95 | 457.6 мс | 207.2 мс | — |
| соединений БД max | 35 | 32 | — |
| лаг стримов max | 52 | 29 | — |
| ошибки / потери / дубли сессий | 0 / 0 / 0 | 0 / 0 / 0 | ✅ |

Песочница остаётся CPU-bound (k6, PostgreSQL, Redis, API и воркеры на 2 vCPU — см. `profile-after.md`), поэтому
абсолютные цифры критерия ТЗ не показательны; относительное улучшение при одинаковой нагрузке — да. Целевой замер —
k6 nightly на staging (A-49, A-62): ожидание по CPU — пропорционально меньше подов на пик 400 соб./с (A-63).

## Что не сделано (осознанно)

- Объединение оставшихся чтений EXIT (сессия + сага + предоплата) в один CTE — три `FOR UPDATE` разных таблиц в одном
  операторе усложняют блокировки ради ещё −2 round-trip; отложено до профиля на staging.
- Redis: ≈ 7 команд на событие (rate limit, ключ, `last_used`, кеш ответа, решение доступа, heartbeat) — не трогались.
- Кросс-подовая инвалидация кешей справочников по интеграционным событиям — не нужна при TTL 10 с; см. выше.
