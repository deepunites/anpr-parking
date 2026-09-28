-- 0003 Devices.
CREATE TABLE camera (
    id                      uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               uuid             NOT NULL,
    lot_id                  uuid             NOT NULL,
    code                    varchar(30)      NOT NULL CHECK (code ~ '^[A-Za-z0-9_-]{1,30}$'),
    name                    varchar(100)     NOT NULL,
    direction               camera_direction NOT NULL,
    vendor                  camera_vendor    NOT NULL DEFAULT 'GENERIC',
    location                text,
    confidence_min          smallint         NOT NULL DEFAULT 80 CHECK (confidence_min BETWEEN 0 AND 100),
    confidence_review_min   smallint         NOT NULL DEFAULT 60 CHECK (confidence_review_min BETWEEN 0 AND 100),
    max_clock_skew_seconds  integer          NOT NULL DEFAULT 120 CHECK (max_clock_skew_seconds > 0),
    heartbeat_interval_sec  integer          NOT NULL DEFAULT 300 CHECK (heartbeat_interval_sec > 0),
    active                  boolean          NOT NULL DEFAULT true,
    status                  camera_status    NOT NULL DEFAULT 'UNKNOWN',
    last_heartbeat_at       timestamptz,
    last_event_at           timestamptz,
    firmware_version        varchar(100),
    created_at              timestamptz      NOT NULL DEFAULT now(),
    updated_at              timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT fk_camera_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_camera_code UNIQUE (lot_id, code),
    CONSTRAINT uq_camera_tenant UNIQUE (tenant_id, id),
    CONSTRAINT uq_camera_tenant_lot UNIQUE (tenant_id, lot_id, id),
    CONSTRAINT ck_confidence_order CHECK (confidence_review_min <= confidence_min)
);
COMMENT ON CONSTRAINT uq_camera_tenant_lot ON camera IS 'Цель FK (tenant_id, lot_id, camera_id): камера другой площадки того же tenant не может стать входной камерой сессии или камерой шлагбаума.';
COMMENT ON COLUMN camera.status IS 'Вычисляется джобом раз в 30 с из Redis-heartbeat (не на каждом heartbeat). SILENT — heartbeat есть, событий нет дольше базовой линии.';
COMMENT ON COLUMN camera.last_heartbeat_at IS 'Батч-флаш из Redis раз в 60 с ролью app_bulk. Прямой UPDATE на каждый heartbeat запрещён (CORE §3).';
CREATE INDEX ix_camera_lot_dir ON camera (lot_id, direction) WHERE active;
CREATE INDEX ix_camera_stale ON camera (last_heartbeat_at NULLS FIRST) WHERE active;
CREATE TRIGGER trg_camera_updated BEFORE UPDATE ON camera FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE camera_api_key (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL,
    camera_id     uuid        NOT NULL,
    key_prefix    varchar(16) NOT NULL CHECK (length(key_prefix) = 16),
    key_hmac      bytea       NOT NULL CHECK (octet_length(key_hmac) = 32),
    pepper_version smallint   NOT NULL DEFAULT 1,
    label         varchar(100),
    active        boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    uuid,
    expires_at    timestamptz,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    revoked_by    uuid,
    CONSTRAINT fk_cak_camera FOREIGN KEY (tenant_id, camera_id) REFERENCES camera (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_cak_created_by FOREIGN KEY (tenant_id, created_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_cak_revoked_by FOREIGN KEY (tenant_id, revoked_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT uq_cak_hmac UNIQUE (key_hmac),
    CONSTRAINT ck_cak_revoked CHECK (revoked_at IS NULL OR NOT active)
);
COMMENT ON TABLE camera_api_key IS 'HMAC-SHA256(pepper из Vault, ключ). Argon2/bcrypt запрещены: 200 RPS, ключ 256 бит энтропии. Сравнение constant-time по всем кандидатам с одним key_prefix.';
COMMENT ON COLUMN camera_api_key.last_used_at IS 'Флаш из Redis раз в 60 с (app_bulk), не на каждом запросе.';
CREATE INDEX ix_cak_lookup ON camera_api_key (key_prefix) WHERE active;

-- Pre-tenant lookup: аутентификация камеры по префиксу ключа до установки tenant-контекста.
CREATE OR REPLACE FUNCTION app.lookup_camera_keys(p_prefix text)
RETURNS TABLE (key_id uuid, tenant_id uuid, camera_id uuid, lot_id uuid, key_hmac bytea, pepper_version smallint,
               expires_at timestamptz, camera_active boolean, direction camera_direction, code varchar,
               confidence_min smallint, confidence_review_min smallint, max_clock_skew_seconds integer,
               late_event_window_hours integer, tenant_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT k.id, k.tenant_id, k.camera_id, c.lot_id, k.key_hmac, k.pepper_version, k.expires_at, c.active,
         c.direction, c.code, c.confidence_min, c.confidence_review_min, c.max_clock_skew_seconds,
         l.late_event_window_hours, t.active
  FROM camera_api_key k
  JOIN camera c ON c.id = k.camera_id
  JOIN parking_lot l ON l.id = c.lot_id
  JOIN tenant t ON t.id = k.tenant_id
  WHERE k.key_prefix = p_prefix AND k.active
$$;
REVOKE ALL ON FUNCTION app.lookup_camera_keys(text) FROM PUBLIC;

CREATE TABLE barrier (
    id                  uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid           NOT NULL,
    lot_id              uuid           NOT NULL,
    camera_id           uuid,
    name                varchar(100)   NOT NULL,
    driver              barrier_driver NOT NULL DEFAULT 'MODBUS_TCP',
    endpoint            varchar(255)   NOT NULL CHECK (length(btrim(endpoint)) > 0),
    auth_secret_ref     varchar(255),
    auto_close_seconds  integer        CHECK (auto_close_seconds IS NULL OR auto_close_seconds > 0),
    active              boolean        NOT NULL DEFAULT true,
    state               barrier_state  NOT NULL DEFAULT 'UNKNOWN',
    last_heartbeat_at   timestamptz,
    created_at          timestamptz    NOT NULL DEFAULT now(),
    updated_at          timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT fk_barrier_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_barrier_camera FOREIGN KEY (tenant_id, lot_id, camera_id) REFERENCES camera (tenant_id, lot_id, id) ON DELETE SET NULL,
    CONSTRAINT uq_barrier_tenant UNIQUE (tenant_id, id),
    CONSTRAINT uq_barrier_tenant_lot UNIQUE (tenant_id, lot_id, id),
    CONSTRAINT ck_barrier_secret CHECK (driver <> 'HTTP_REST' OR auth_secret_ref IS NOT NULL)
);
COMMENT ON COLUMN barrier.endpoint IS 'host:port (Modbus) или URL (HTTP). Проверяется в приложении против parking_lot.barrier_cidr_allowlist и блок-листа приватных/метаданных адресов (SSRF, фаза 4).';
COMMENT ON COLUMN barrier.auth_secret_ref IS 'Ссылка на секрет в Vault; не секрет и не хеш — им нужно аутентифицироваться в исходящем запросе.';
CREATE TRIGGER trg_barrier_updated BEFORE UPDATE ON barrier FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Команды шлагбаумам: ~2 на сессию → до 2 млн/сутки. Партиции по месяцам, ретеншен 13 месяцев.
CREATE TABLE barrier_command (
    id                uuid                   NOT NULL DEFAULT gen_random_uuid(),
    created_at        timestamptz            NOT NULL DEFAULT now(),
    tenant_id         uuid                   NOT NULL,
    lot_id            uuid                   NOT NULL,
    barrier_id        uuid                   NOT NULL,
    kind              barrier_command_kind   NOT NULL,
    state             barrier_command_state  NOT NULL DEFAULT 'PENDING',
    source            barrier_command_source NOT NULL,
    idempotency_key   bytea                  NOT NULL CHECK (octet_length(idempotency_key) = 32),
    correlation_id    uuid                   NOT NULL,
    session_id        uuid,
    saga_id           uuid,
    issued_by         uuid,
    reason            text,
    attempts          smallint               NOT NULL DEFAULT 0,
    last_error        text,
    sent_at           timestamptz,
    completed_at      timestamptz,
    latency_ms        integer,
    PRIMARY KEY (created_at, id),
    CONSTRAINT fk_bc_barrier FOREIGN KEY (tenant_id, lot_id, barrier_id) REFERENCES barrier (tenant_id, lot_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_bc_issued_by FOREIGN KEY (tenant_id, issued_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT ck_bc_manual_reason CHECK (source <> 'MANUAL' OR (issued_by IS NOT NULL AND length(btrim(coalesce(reason, ''))) >= 3))
) PARTITION BY RANGE (created_at);
COMMENT ON TABLE barrier_command IS 'Команда пишется ДО отправки (PENDING), результат — после (OK/FAILED), в разных транзакциях (I-9). Ручная команда обязана иметь issued_by и reason (I-15).';
COMMENT ON COLUMN barrier_command.idempotency_key IS 'saga_id+step или session_id+ENTRY. Глобальная уникальность на партициях невозможна; повтор исключается блокировкой саги/сессии в той же транзакции.';
CREATE INDEX ix_bc_barrier_time ON barrier_command (barrier_id, created_at DESC);
CREATE INDEX ix_bc_inflight ON barrier_command (created_at) WHERE state IN ('PENDING', 'SENT');
CREATE INDEX ix_bc_idem ON barrier_command (idempotency_key);
CREATE INDEX ix_bc_session ON barrier_command (session_id) WHERE session_id IS NOT NULL;
