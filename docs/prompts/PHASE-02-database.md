# PHASE 02 — База данных

**Предусловие:** gate фазы 1 пройден.
**Выход:** DDL, миграции Alembic, RLS, партиции, `docs/DATABASE.md`, тесты на инварианты уровня БД.

---

## 0. Первое задание фазы: разнести черновик

В §5 этого файла лежит **черновик схемы v2**. Он написан специально для критики и содержит нерешённые места, помеченные `-- ОТКРЫТО`.

**Не реализуй его как есть.** Сначала выведи разбор:

```
### Критика черновика
Проблема N: <что не так>
Почему это сломается: <конкретный сценарий>
Исправление: <DDL>
```

Проверь минимум это:

- Каждая ли tenant-owned таблица имеет `tenant_id` и защищена от рассинхронизации с родителем.
- Возможна ли межтенантная связь через FK (например, камера одного tenant привязана к площадке другого).
- Работают ли заявленные ограничения на партиционированных таблицах — **проверь эмпирически**, не по памяти.
- Достаточны ли индексы для запросов из §3.
- Нет ли циклических FK.
- Нет ли места, где номер машины остаётся в открытом виде.
- Корректны ли типы под объёмы из §3 CORE.

Только после разбора — миграции.

---

## 1. Решения, которые принимаются в этой фазе

### 1.1 Партиционирование `parking_session` против инварианта I-1

Конфликт, который нужно разрешить явно:

- 1 000 000 сессий в сутки → 365 млн строк в год. Без партиционирования таблица станет неуправляемой.
- I-1 требует `UNIQUE (lot_id, vehicle_id) WHERE status = 'ACTIVE'`.
- PostgreSQL требует, чтобы уникальность на партиционированной таблице включала все колонки ключа партиционирования.

Два пути:

**Вариант A — HASH-партиционирование по `lot_id`.** Ключ партиционирования входит в уникальный индекс, требование формально выполняется. Даёт равномерное распределение. Минус: нельзя дропать партиции по времени, ретеншен придётся делать через DELETE.

**Вариант B — горячая/холодная разбивка.** Небольшая непартиционированная `parking_session_active` (≈ 50 000 строк, живёт только пока сессия открыта) с обычным `UNIQUE (lot_id, vehicle_id)`, и партиционированная по месяцам `parking_session` для закрытых. Инвариант держится на маленькой таблице; ретеншен — дроп партиций. Минус: две таблицы, перенос при закрытии сессии, усложнение запросов.

**Проверено на PostgreSQL 16.13 — используй как отправную точку, но воспроизведи на своей версии и вставь вывод:**

```
=== partial UNIQUE на HASH-партиционированной таблице, ключ партиционирования включён ===
CREATE UNIQUE INDEX uq_active ON ps (lot_id, vehicle_id) WHERE status = 'ACTIVE';
CREATE INDEX                          ← работает

=== принуждение ===
INSERT ... 'ACTIVE'  → INSERT 0 1
INSERT ... 'ACTIVE'  → ERROR: duplicate key value violates unique constraint "ps_0_lot_id_vehicle_id_idx"
INSERT ... 'CLOSED'  → INSERT 0 1
INSERT ... 'CLOSED'  → INSERT 0 1     ← две закрытые сессии разрешены, инвариант держится

=== то же без ключа партиционирования ===
CREATE UNIQUE INDEX uq_bad ON ps (vehicle_id) WHERE status = 'ACTIVE';
ERROR: unique constraint on partitioned table must include all partitioning columns

=== EXCLUDE на партиционированной таблице ===
ERROR: exclusion constraints are not supported on partitioned tables
```

**Вывод:** вариант A работоспособен. Но это не делает выбор автоматическим — сопоставь его с ретеншеном (партиции по хешу нельзя дропать по времени) и оформи ADR с обоснованием.

`EXCLUDE` не поддерживается на партиционированных таблицах вообще. Значит `tariff_plan_version` (I-12) партиционировать нельзя — это ограничение, а не выбор.

### 1.2 Уникальность идемпотентности против партиционирования

`anpr_event` по объёму просится в партиции по месяцам. Но I-3 требует глобальной уникальности `idempotency_key`, а уникальность на партиционированной таблице возможна только с ключом партиционирования в составе — то есть дубликат, пришедший на границе партиций, пролезет.

Решение в черновике: **отдельная непартиционированная таблица-реестр `anpr_idempotency`**, ретеншен 7 суток. Обоснование: `event_time` входит в ключ, допустимый клоковый разбег ограничен минутами (§4 фазы 4), значит дубликат физически не может прийти спустя неделю. Объём: 2.5 млн × 7 ≈ 17 млн строк.

Проверь это рассуждение и подтверди или опровергни. Если опровергаешь — дай альтернативу.

### 1.3 RLS: как приложение представляется базе

Реши и обоснуй:

- роль приложения, для которой RLS не обходится (`NOLOGIN BYPASSRLS` — запрещено);
- как проставляется `app.current_tenant_id`: `SET LOCAL` на каждую транзакцию;
- что происходит при отсутствии переменной — политика должна закрывать доступ, а не открывать;
- как работают фоновые джобы, которым нужен доступ к нескольким tenant (отдельная роль с явной политикой, не BYPASSRLS);
- взаимодействие RLS с пулером соединений: `SET LOCAL` внутри транзакции безопасен, `SET` вне — нет. Зафиксируй это как правило.

RLS — **второй** рубеж. Первый — обязательная фильтрация по `tenant_id` в репозиториях. Тест должен проверять оба независимо.

### 1.4 Шифрование номеров

- `plate_hash` = HMAC-SHA256(per-tenant HMAC-ключ, нормализованный номер). Детерминирован, по нему ищем.
- `plate_encrypted` = AES-256-GCM(per-tenant DEK, номер). Недетерминирован (nonce), по нему не ищем.
- Ключи — в Vault Transit, в БД только `key_version SMALLINT`.
- Нормализация номера (регистр, кириллица/латиница, пробелы, дефисы) выполняется **до** хеширования и является частью доменного слоя. Опиши правила нормализации для узбекских номеров явно и покрой тестами: `01 A 123 AA`, `01a123aa`, `01А123АА` (кириллица) должны дать один хеш.
- Ротация ключа: фоновая перешифровка с версионированием, поиск в переходный период по хешам обеих версий.

---

## 2. Что произвести

```
[ ] Полный DDL с комментариями COMMENT ON к нетривиальным местам
[ ] ERD (Mermaid) в docs/DATABASE.md
[ ] Миграции Alembic, применяемые с нуля и откатываемые
[ ] RLS-политики на все tenant-owned таблицы
[ ] Функции и скрипты управления партициями (создание вперёд, дроп по ретеншену)
[ ] Сид-данные для разработки: 2 tenant, 3 lot, 6 камер, 2 шлагбаума, тарифы
[ ] Тесты на инварианты I-1, I-2, I-3, I-6, I-11, I-12
```

---

## 3. Запросы, под которые обязаны быть индексы

Каждый — с `EXPLAIN (ANALYZE, BUFFERS)` на сид-данных объёмом не менее 1 млн сессий. Index Scan обязателен, Seq Scan на горячем пути — провал gate.

1. Поиск активной сессии по `(tenant_id, lot_id, plate_hash)` — горячий путь EXIT.
2. Проверка попадания в blacklist по `(tenant_id, plate_hash)` с учётом `lot_id IS NULL` как глобального правила.
3. То же для whitelist, VIP, subscription с проверкой срока действия на момент события.
4. Проверка идемпотентности по `idempotency_key`.
5. Выборка незаопубликованных записей outbox с блокировкой `FOR UPDATE SKIP LOCKED`.
6. Occupancy: число активных сессий по `lot_id`.
7. Дневной отчёт: платежи за диапазон дат по tenant с группировкой по способу оплаты.
8. Лента сессий оператора: последние 50 по площадке с пагинацией по курсору, не по OFFSET.
9. Камеры, не присылавшие heartbeat дольше порога.

---

## 4. Ретеншен

| Данные | Срок | Механизм |
|---|---|---|
| Изображения в MinIO | 90 дней | lifecycle policy бакета |
| `event_log` | 3 года | дроп партиции |
| `audit_log` | 5 лет | дроп партиции |
| `payment`, `payment_attempt`, `refund` | 7 лет | не удаляются |
| `anpr_idempotency` | 7 суток | DELETE по индексу + агрессивный autovacuum |
| `parking_session` | 7 лет (финансовая связь) | по выбранному варианту §1.1 |
| Redis | TTL | — |

Джоб ретеншена обязан быть идемпотентным, наблюдаемым (метрика + лог) и не выполнять `DELETE` там, где возможен дроп партиции.

---

## 5. Черновик схемы v2 — для критики, не для копирования

> Исправлено относительно v1: добавлен `tenant_id` в дочерние таблицы, композитные FK против межтенантных связей, убран открытый номер, разделены тарифный план и версия, секрет шлагбаума перестал быть хешем, разорвана циклическая FK, PK партиционированных таблиц включают ключ партиционирования, добавлен реестр идемпотентности и outbox.
>
> **Черновик выполнен на PostgreSQL 16.13 с `ON_ERROR_STOP=1` — применяется без ошибок.** Композитная FK проверена: попытка привязать камеру tenant B к площадке tenant A отвергается базой (`ERROR: insert or update on table "camera" violates foreign key constraint "fk_camera_lot"`). Синтаксическая валидность не означает архитектурную правильность — разбор из §0 всё равно обязателен.

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ============================================================
-- TENANT
-- ============================================================
CREATE TABLE tenant (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code          VARCHAR(30)  NOT NULL UNIQUE,
    name          VARCHAR(150) NOT NULL,
    legal_name    VARCHAR(255),
    currency      CHAR(3)      NOT NULL DEFAULT 'UZS',
    timezone      VARCHAR(50)  NOT NULL DEFAULT 'Asia/Tashkent',
    -- Ключей здесь нет. Только ссылка на путь в Vault и активная версия.
    crypto_key_ref     VARCHAR(255) NOT NULL,
    crypto_key_version SMALLINT     NOT NULL DEFAULT 1,
    active        BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ============================================================
-- PARKING LOT
-- ============================================================
CREATE TABLE parking_lot (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID         NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    code       VARCHAR(20)  NOT NULL,
    name       VARCHAR(100) NOT NULL,
    address    TEXT,
    capacity   INT          NOT NULL CHECK (capacity > 0),
    timezone   VARCHAR(50)  NOT NULL,
    active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_lot_code UNIQUE (tenant_id, code),
    -- Нужен для композитных FK из дочерних таблиц: они ссылаются на
    -- (tenant_id, lot_id) и тем самым физически не могут смотреть в чужой tenant.
    CONSTRAINT uq_lot_tenant UNIQUE (tenant_id, id)
);
CREATE INDEX ix_lot_tenant_active ON parking_lot (tenant_id, active);

-- ============================================================
-- CAMERA
-- ============================================================
CREATE TYPE camera_direction AS ENUM ('IN', 'OUT');
CREATE TYPE camera_status    AS ENUM ('ONLINE', 'DEGRADED', 'OFFLINE');

CREATE TABLE camera (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL,
    lot_id     UUID NOT NULL,
    code       VARCHAR(30)  NOT NULL,
    name       VARCHAR(100) NOT NULL,
    direction  camera_direction NOT NULL,
    location   TEXT,
    -- Было требование "confidence policy configurable per camera",
    -- но в v1 колонки под него не существовало.
    confidence_min          SMALLINT NOT NULL DEFAULT 80
                            CHECK (confidence_min BETWEEN 0 AND 100),
    confidence_review_min   SMALLINT NOT NULL DEFAULT 60
                            CHECK (confidence_review_min BETWEEN 0 AND 100),
    max_clock_skew_seconds  INT      NOT NULL DEFAULT 120 CHECK (max_clock_skew_seconds > 0),
    heartbeat_interval_sec  INT      NOT NULL DEFAULT 300 CHECK (heartbeat_interval_sec > 0),
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    status     camera_status NOT NULL DEFAULT 'OFFLINE',
    last_heartbeat_at TIMESTAMPTZ,
    firmware_version  VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_camera_lot FOREIGN KEY (tenant_id, lot_id)
        REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_camera_code UNIQUE (lot_id, code),
    CONSTRAINT uq_camera_tenant UNIQUE (tenant_id, id),
    CONSTRAINT ck_confidence_order CHECK (confidence_review_min <= confidence_min)
);
CREATE INDEX ix_camera_lot_dir ON camera (lot_id, direction) WHERE active;
CREATE INDEX ix_camera_stale   ON camera (last_heartbeat_at) WHERE active;

-- ============================================================
-- CAMERA API KEY
-- ВАЖНО: для входящей аутентификации на 200 RPS нельзя Argon2/bcrypt.
-- HMAC-SHA256 с pepper из Vault + сравнение constant-time.
-- ============================================================
CREATE TABLE camera_api_key (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL,
    camera_id  UUID NOT NULL,
    key_hmac   BYTEA       NOT NULL,          -- 32 байта, HMAC-SHA256
    key_prefix VARCHAR(16) NOT NULL,          -- для поиска и для отображения в UI
    label      VARCHAR(100),
    active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    revoked_by   UUID,
    CONSTRAINT fk_cak_camera FOREIGN KEY (tenant_id, camera_id)
        REFERENCES camera (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_cak_hmac UNIQUE (key_hmac),
    CONSTRAINT ck_cak_revoked CHECK (revoked_at IS NULL OR NOT active)
);
CREATE INDEX ix_cak_lookup ON camera_api_key (key_prefix) WHERE active;
-- ОТКРЫТО: обновление last_used_at на каждом запросе — это 200 UPDATE/s
-- на горячую таблицу. Предложи решение (Redis + батч-флаш) и обоснуй интервал.

-- ============================================================
-- BARRIER
-- ВАЖНО: v1 хранила auth_token_hash. Хешем невозможно аутентифицироваться
-- в ИСХОДЯЩЕМ запросе к контроллеру. Нужна ссылка на секрет.
-- ============================================================
CREATE TYPE barrier_state AS ENUM
    ('OPEN','CLOSED','OPENING','CLOSING','UNKNOWN','ERROR');

CREATE TABLE barrier (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL,
    lot_id     UUID NOT NULL,
    camera_id  UUID,
    name       VARCHAR(100) NOT NULL,
    endpoint   VARCHAR(255) NOT NULL,
    auth_secret_ref VARCHAR(255) NOT NULL,     -- путь в Vault, не секрет и не хеш
    driver     VARCHAR(50)  NOT NULL DEFAULT 'http_rest',
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    state      barrier_state NOT NULL DEFAULT 'UNKNOWN',
    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_barrier_lot FOREIGN KEY (tenant_id, lot_id)
        REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_barrier_camera FOREIGN KEY (tenant_id, camera_id)
        REFERENCES camera (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT uq_barrier_tenant UNIQUE (tenant_id, id)
);
-- ОТКРЫТО: endpoint задаётся администратором tenant и используется для
-- исходящего запроса. Это готовый SSRF. Опиши в фазе 4 аллоклист
-- (частные подсети площадки), запрет редиректов, резолв DNS до подключения.

-- ============================================================
-- VEHICLE
-- Открытого номера нет ни в одной колонке.
-- ============================================================
CREATE TABLE vehicle (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID  NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    plate_hash    BYTEA NOT NULL,              -- HMAC-SHA256, 32 байта
    plate_encrypted BYTEA NOT NULL,            -- AES-256-GCM (nonce||ct||tag)
    key_version   SMALLINT NOT NULL DEFAULT 1,
    country_code  CHAR(2),                     -- не ПДн, нужен для аналитики
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_vehicle_plate UNIQUE (tenant_id, plate_hash),
    CONSTRAINT uq_vehicle_tenant UNIQUE (tenant_id, id)
);

-- ============================================================
-- TARIFF
-- v1 имела одну колонку price_per_hour, но требовала grace period,
-- daily max, overnight, выходные и праздники. Разделено на план,
-- версию и правила.
-- ============================================================
CREATE TABLE tariff_plan (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL,
    lot_id     UUID NOT NULL,
    code       VARCHAR(30)  NOT NULL,
    name       VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_plan_lot FOREIGN KEY (tenant_id, lot_id)
        REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_plan_code UNIQUE (lot_id, code)
);

CREATE TABLE tariff_plan_version (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL,
    plan_id    UUID NOT NULL REFERENCES tariff_plan(id) ON DELETE RESTRICT,
    version    INT  NOT NULL,
    currency   CHAR(3) NOT NULL DEFAULT 'UZS',
    -- Деньги — BIGINT в минорных единицах (тийин). Никакого NUMERIC и float.
    rules      JSONB NOT NULL,
    validity   TSTZRANGE NOT NULL,
    created_by UUID  NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tpv UNIQUE (plan_id, version),
    -- I-12. EXCLUDE не работает на партиционированных таблицах,
    -- поэтому эта таблица остаётся непартиционированной.
    CONSTRAINT ex_tpv_no_overlap EXCLUDE USING gist
        (plan_id WITH =, validity WITH &&)
);
-- ОТКРЫТО: rules в JSONB даёт гибкость, но снимает проверку типов с БД.
-- Требуется JSON Schema, валидация в домене и CHECK через jsonb_matches_schema
-- (расширение pg_jsonschema) либо триггер. Выбери и обоснуй.
-- Опиши структуру rules: grace_minutes, first_hour_price, price_per_hour,
-- daily_max, overnight_window, rounding_mode, weekday_overrides, holiday_calendar_id.

-- ============================================================
-- PARKING SESSION
-- Ключ партиционирования определяется по итогам §1.1.
-- Ниже — вариант A (HASH по lot_id).
-- ============================================================
CREATE TYPE session_status AS ENUM
    ('ACTIVE','CLOSED','ORPHAN','MANUAL_REVIEW','CANCELLED');

CREATE TABLE parking_session (
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    lot_id        UUID NOT NULL,
    vehicle_id    UUID NOT NULL,
    entry_camera_id UUID NOT NULL,
    exit_camera_id  UUID,
    entry_time    TIMESTAMPTZ NOT NULL,
    exit_time     TIMESTAMPTZ,
    -- Хранимое поле, а не GENERATED: exit_time может быть скорректирован
    -- вручную, и billing_minutes не обязано совпадать с фактической разницей.
    billing_minutes INT,
    tariff_plan_version_id UUID REFERENCES tariff_plan_version(id),
    status        session_status NOT NULL DEFAULT 'ACTIVE',
    -- payment_id УБРАН: в v1 была циклическая FK
    -- parking_session.payment_id <-> payment.session_id.
    -- Направление одно: payment -> session.
    version       INT NOT NULL DEFAULT 1,      -- оптимистичная блокировка
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lot_id, id),
    CONSTRAINT ck_exit_after_entry CHECK (exit_time IS NULL OR exit_time >= entry_time),
    CONSTRAINT ck_closed_has_exit
        CHECK (status <> 'CLOSED' OR (exit_time IS NOT NULL AND billing_minutes IS NOT NULL))
) PARTITION BY HASH (lot_id);
-- создать 32 партиции

-- I-1. ПРОВЕРИТЬ ЭМПИРИЧЕСКИ на PostgreSQL 16 и вставить вывод psql:
CREATE UNIQUE INDEX uq_session_one_active
    ON parking_session (lot_id, vehicle_id) WHERE status = 'ACTIVE';

CREATE INDEX ix_session_lookup ON parking_session (lot_id, vehicle_id, status);
CREATE INDEX ix_session_tenant_time ON parking_session (tenant_id, entry_time DESC);
CREATE INDEX ix_session_open ON parking_session (lot_id, entry_time)
    WHERE status = 'ACTIVE';

-- ============================================================
-- ИДЕМПОТЕНТНОСТЬ ANPR
-- Непартиционированная по причине из §1.2.
-- ============================================================
CREATE TABLE anpr_idempotency (
    idempotency_key BYTEA PRIMARY KEY,         -- SHA-256, 32 байта
    tenant_id   UUID NOT NULL,
    result_kind VARCHAR(40) NOT NULL,          -- что вернули в первый раз
    result_ref  UUID,                          -- id сессии/события
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_idem_gc ON anpr_idempotency (created_at);

-- ============================================================
-- OUTBOX
-- ============================================================
CREATE TYPE outbox_status AS ENUM ('PENDING','PUBLISHED','FAILED');

CREATE TABLE outbox (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   UUID NOT NULL,
    aggregate_type VARCHAR(50) NOT NULL,
    aggregate_id   UUID NOT NULL,
    event_type  VARCHAR(80) NOT NULL,          -- parking.session.created.v1
    payload     JSONB NOT NULL,
    correlation_id UUID NOT NULL,
    trace_id    VARCHAR(64),
    status      outbox_status NOT NULL DEFAULT 'PENDING',
    retry_count SMALLINT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
CREATE INDEX ix_outbox_pending ON outbox (created_at)
    WHERE status = 'PENDING';
CREATE INDEX ix_outbox_retry ON outbox (next_retry_at)
    WHERE status = 'FAILED';

-- ============================================================
-- PAYMENT
-- ============================================================
CREATE TYPE payment_status AS ENUM
    ('PENDING','PAID','CANCELLED','REFUNDED','FAILED');
CREATE TYPE payment_method AS ENUM
    ('CASH','CARD','ONLINE','SUBSCRIPTION','VIP');

CREATE TABLE payment (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    lot_id      UUID NOT NULL,
    session_id  UUID NOT NULL,
    amount      BIGINT NOT NULL CHECK (amount >= 0),   -- тийины
    currency    CHAR(3) NOT NULL DEFAULT 'UZS',
    status      payment_status NOT NULL DEFAULT 'PENDING',
    method      payment_method NOT NULL,
    operator_id UUID,
    calculation_snapshot JSONB NOT NULL,   -- I-7: аудит расчёта
    idempotency_key BYTEA NOT NULL,
    paid_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_payment_idem UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT ck_cash_has_operator
        CHECK (method <> 'CASH' OR operator_id IS NOT NULL),
    CONSTRAINT ck_paid_has_time
        CHECK (status <> 'PAID' OR paid_at IS NOT NULL)
);
CREATE INDEX ix_payment_session ON payment (session_id);
CREATE INDEX ix_payment_report  ON payment (tenant_id, paid_at, method)
    WHERE status = 'PAID';

-- I-6: триггер, запрещающий изменение терминальных платежей.
-- Реализовать как BEFORE UPDATE, разрешающий переход только
-- PENDING -> {PAID, FAILED, CANCELLED} и PAID -> REFUNDED.

-- ============================================================
-- EVENT LOG / AUDIT LOG — партиции по месяцам.
-- PK обязан включать ключ партиционирования.
-- ============================================================
CREATE TABLE audit_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id  UUID NOT NULL,
    user_id    UUID,
    action     VARCHAR(50) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id  UUID,
    reason     TEXT,                     -- I-15
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address INET,
    user_agent TEXT,
    correlation_id UUID NOT NULL,
    PRIMARY KEY (created_at, id)          -- было PK(id) — на партициях невозможно
) PARTITION BY RANGE (created_at);

-- I-11: append-only
-- REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM app_role;
-- GRANT INSERT, SELECT ON audit_log TO app_role;
```

**Не показаны намеренно** — спроектируй сам по тем же правилам, и объясни каждое решение:
`user`, `refresh_token`, `subscription`, `vip_access`, `blacklist`, `whitelist`,
`parking_spot`, `event_log`, `payment_attempt`, `refund`, `payment_reconciliation`,
`barrier_command`, `gdpr_deletion_request`, `manual_correction`.

Для списков доступа (`blacklist`, `whitelist`, `vip_access`, `subscription`) отдельно реши: как одним запросом проверить и глобальное правило tenant (`lot_id IS NULL`), и правило площадки, и попасть при этом в индекс.

---

## 6. Gate фазы 2

```
[ ] Разбор черновика выведен, каждая найденная проблема исправлена в DDL
[ ] alembic upgrade head на пустой БД — вывод вставлен
[ ] alembic downgrade base && alembic upgrade head — вывод вставлен
[ ] Вывод psql по эксперименту из §1.1 вставлен, ADR написан
[ ] Сид-данные загружены, сгенерирован 1 млн сессий
[ ] EXPLAIN (ANALYZE, BUFFERS) по всем 9 запросам §3 — вывод вставлен, Seq Scan отсутствует
[ ] Тест: сессия tenant A не видна из-под tenant B ни через репозиторий, ни через прямой SQL под ролью приложения
[ ] Тест: две параллельные транзакции не создают две ACTIVE сессии на одну пару (lot, vehicle)
[ ] Тест: UPDATE payment в статусе PAID отклоняется
[ ] Тест: DELETE из audit_log под ролью приложения отклоняется
[ ] Тест: пересекающиеся версии тарифа отклоняются
[ ] grep по всей схеме: ни одной колонки с открытым номером
[ ] docs/DATABASE.md: ERD, ретеншен, стратегия партиционирования, RLS
```
