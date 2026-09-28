-- 0002 Tenancy + Identity.
CREATE TABLE tenant (
    id                  uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    code                varchar(30)  NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9_-]{1,29}$'),
    name                varchar(150) NOT NULL,
    legal_name          varchar(255),
    currency            char(3)      NOT NULL DEFAULT 'UZS' CHECK (currency = 'UZS'),
    timezone            varchar(50)  NOT NULL DEFAULT 'Asia/Tashkent',
    crypto_key_ref      varchar(255) NOT NULL,
    enc_key_version     smallint     NOT NULL DEFAULT 1 CHECK (enc_key_version > 0),
    hmac_key_version    smallint     NOT NULL DEFAULT 1 CHECK (hmac_key_version > 0),
    active              boolean      NOT NULL DEFAULT true,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now()
);
COMMENT ON TABLE tenant IS 'Организация-оператор парковок. Корень изоляции (I-2).';
COMMENT ON COLUMN tenant.currency IS 'Q8: только UZS; CHECK снимается вместе с реализацией мультивалютности.';
COMMENT ON COLUMN tenant.crypto_key_ref IS 'Путь в Vault KV к обёрнутым (Transit) DEK и HMAC-ключу tenant. Ключевого материала в БД нет (A-05).';
COMMENT ON COLUMN tenant.enc_key_version IS 'Активная версия DEK (AES-256-GCM). Строки с меньшей версией перешифровываются фоновым джобом.';
COMMENT ON COLUMN tenant.hmac_key_version IS 'Активная версия HMAC-ключа plate_hash. В переходный период поиск идёт по хешам обеих версий.';
CREATE TRIGGER trg_tenant_updated BEFORE UPDATE ON tenant FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE parking_lot (
    id                          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   uuid         NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    code                        varchar(20)  NOT NULL,
    name                        varchar(100) NOT NULL,
    address                     text,
    capacity                    integer      NOT NULL CHECK (capacity > 0),
    timezone                    varchar(50)  NOT NULL,
    active                      boolean      NOT NULL DEFAULT true,
    -- политики площадки (ASSUMPTIONS A-07, A-08, A-10, A-11, A-12, A-13)
    max_session_hours           integer      NOT NULL DEFAULT 72  CHECK (max_session_hours BETWEEN 1 AND 24 * 365),
    reentry_window_minutes      integer      NOT NULL DEFAULT 5   CHECK (reentry_window_minutes BETWEEN 0 AND 60),
    unknown_exit_policy         unknown_exit_policy NOT NULL DEFAULT 'MANUAL_REVIEW',
    deny_entry_when_full        boolean      NOT NULL DEFAULT false,
    exit_grace_minutes          integer      NOT NULL DEFAULT 15  CHECK (exit_grace_minutes BETWEEN 0 AND 240),
    exit_inferred_after_minutes integer      NOT NULL DEFAULT 60  CHECK (exit_inferred_after_minutes BETWEEN 1 AND 1440),
    late_event_window_hours     integer      NOT NULL DEFAULT 24  CHECK (late_event_window_hours BETWEEN 1 AND 168),
    barrier_cidr_allowlist      cidr[]       NOT NULL DEFAULT '{}',
    created_at                  timestamptz  NOT NULL DEFAULT now(),
    updated_at                  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_lot_code   UNIQUE (tenant_id, code),
    CONSTRAINT uq_lot_tenant UNIQUE (tenant_id, id)
);
COMMENT ON CONSTRAINT uq_lot_tenant ON parking_lot IS 'Цель композитных FK (tenant_id, lot_id) из дочерних таблиц: межтенантная ссылка невозможна физически.';
COMMENT ON COLUMN parking_lot.barrier_cidr_allowlist IS 'SSRF-аллоклист подсетей контроллеров шлагбаумов на уровне площадки (фаза 4).';
COMMENT ON COLUMN parking_lot.late_event_window_hours IS 'A-13: event_time старше окна отклоняется как replay; старше 10 мин, но в окне — принимается как late.';
CREATE INDEX ix_lot_tenant_active ON parking_lot (tenant_id) WHERE active;
CREATE TRIGGER trg_lot_updated BEFORE UPDATE ON parking_lot FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE parking_spot (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid        NOT NULL,
    lot_id      uuid        NOT NULL,
    code        varchar(20) NOT NULL,
    zone        varchar(30),
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_spot_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_spot_code UNIQUE (lot_id, code)
);
COMMENT ON TABLE parking_spot IS 'Справочник мест. В occupancy не участвует (ADR-05): занятость = число ACTIVE-сессий.';
CREATE TRIGGER trg_spot_updated BEFORE UPDATE ON parking_spot FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app_user (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid         NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    email           varchar(254) NOT NULL CHECK (email = lower(email) AND position('@' in email) > 1),
    password_hash   text         NOT NULL,
    role            user_role    NOT NULL,
    display_name    varchar(100) NOT NULL,
    locale          varchar(5)   NOT NULL DEFAULT 'ru' CHECK (locale IN ('ru', 'uz')),
    token_version   integer      NOT NULL DEFAULT 1,
    active          boolean      NOT NULL DEFAULT true,
    failed_logins   smallint     NOT NULL DEFAULT 0,
    locked_until    timestamptz,
    last_login_at   timestamptz,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_email  UNIQUE (email),
    CONSTRAINT uq_user_tenant UNIQUE (tenant_id, id)
);
COMMENT ON TABLE app_user IS 'Пользователи (операторы, кассиры, админы). Пароль — argon2id. Права разворачиваются из role в коде, в токен не кладутся.';
COMMENT ON COLUMN app_user.email IS 'Глобально уникален (нормализован в lower): логин без указания tenant, tenant определяется по пользователю.';
COMMENT ON COLUMN app_user.token_version IS 'Увеличивается при смене роли/пароля/отзыве — access-токены с меньшей версией отвергаются.';
CREATE TRIGGER trg_user_updated BEFORE UPDATE ON app_user FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE user_lot (
    tenant_id   uuid NOT NULL,
    user_id     uuid NOT NULL,
    lot_id      uuid NOT NULL,
    PRIMARY KEY (user_id, lot_id),
    CONSTRAINT fk_user_lot_user FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_user_lot_lot  FOREIGN KEY (tenant_id, lot_id)  REFERENCES parking_lot (tenant_id, id) ON DELETE CASCADE
);
COMMENT ON TABLE user_lot IS 'Скоуп площадок пользователя. Пусто — все площадки tenant (для TENANT_ADMIN/MANAGER). Применяется в репозиториях, не в роутах.';

CREATE TABLE refresh_token (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL,
    user_id         uuid        NOT NULL,
    family_id       uuid        NOT NULL,
    token_hash      bytea       NOT NULL CHECK (octet_length(token_hash) = 32),
    issued_at       timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    used_at         timestamptz,
    replaced_by     uuid,
    revoked_at      timestamptz,
    revoke_reason   varchar(40),
    ip_address      inet,
    user_agent      text,
    CONSTRAINT fk_rt_user FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_rt_hash UNIQUE (token_hash)
);
COMMENT ON TABLE refresh_token IS 'Семейства refresh-токенов. Предъявление уже использованного (used_at IS NOT NULL) — reuse: отзывается всё семейство (family_id).';
CREATE INDEX ix_rt_family ON refresh_token (family_id);
CREATE INDEX ix_rt_user_live ON refresh_token (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_rt_expiry ON refresh_token (expires_at);

CREATE TABLE login_attempt (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email_hash    bytea       NOT NULL CHECK (octet_length(email_hash) = 32),
    ip_address    inet,
    success       boolean     NOT NULL,
    attempted_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE login_attempt IS 'Антибрутфорс по (email, ip) и по ip. Без tenant_id: попытка может быть по несуществующему пользователю; email хранится хешем.';
CREATE INDEX ix_login_attempt_email ON login_attempt (email_hash, attempted_at DESC);
CREATE INDEX ix_login_attempt_ip ON login_attempt (ip_address, attempted_at DESC);

-- Pre-tenant lookup: логин по email до того, как известен tenant. SECURITY DEFINER — единственный путь чтения app_user без tenant-контекста.
CREATE OR REPLACE FUNCTION app.lookup_user_by_email(p_email text)
RETURNS TABLE (id uuid, tenant_id uuid, password_hash text, role user_role, token_version integer, active boolean,
               failed_logins smallint, locked_until timestamptz, tenant_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT u.id, u.tenant_id, u.password_hash, u.role, u.token_version, u.active, u.failed_logins, u.locked_until, t.active
  FROM app_user u JOIN tenant t ON t.id = u.tenant_id
  WHERE u.email = lower(p_email)
$$;
REVOKE ALL ON FUNCTION app.lookup_user_by_email(text) FROM PUBLIC;
