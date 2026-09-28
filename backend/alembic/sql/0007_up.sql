-- 0007 Audit, event_log, outbox, consumer_inbox, media, privacy.
CREATE TABLE audit_log (
    id              bigint      GENERATED ALWAYS AS IDENTITY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    tenant_id       uuid        NOT NULL,
    actor_type      actor_type  NOT NULL,
    actor_id        uuid,
    user_id         uuid,
    action          varchar(50) NOT NULL,
    entity_type     varchar(50) NOT NULL,
    entity_id       uuid,
    lot_id          uuid,
    session_id      uuid,
    reason          text,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ip_address      inet,
    user_agent      text,
    correlation_id  uuid        NOT NULL,
    PRIMARY KEY (created_at, id),
    CONSTRAINT ck_audit_user_reason CHECK (actor_type <> 'USER' OR (user_id IS NOT NULL AND length(btrim(coalesce(reason, ''))) >= 3))
) PARTITION BY RANGE (created_at);
COMMENT ON TABLE audit_log IS 'Append-only (I-11): ролям приложения выданы только INSERT и SELECT. Действие пользователя обязано иметь user_id и reason (I-15). Ретеншен 5 лет дропом партиций.';
CREATE INDEX ix_audit_tenant_time ON audit_log (tenant_id, created_at DESC);
CREATE INDEX ix_audit_entity ON audit_log (entity_type, entity_id, created_at DESC);
CREATE INDEX ix_audit_user ON audit_log (user_id, created_at DESC) WHERE user_id IS NOT NULL;
SELECT app.ensure_range_partitions('audit_log', 'month', 1, 2);

CREATE TABLE event_log (
    id              bigint      GENERATED ALWAYS AS IDENTITY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    tenant_id       uuid        NOT NULL,
    event_id        uuid        NOT NULL,
    event_type      varchar(80) NOT NULL,
    aggregate_type  varchar(50) NOT NULL,
    aggregate_id    uuid        NOT NULL,
    payload         jsonb       NOT NULL,
    correlation_id  uuid        NOT NULL,
    PRIMARY KEY (created_at, id)
) PARTITION BY RANGE (created_at);
COMMENT ON TABLE event_log IS 'Журнал integration events для аналитики и разбора инцидентов (пул app_bulk, RPO ≤ 5 мин). Ретеншен 3 года дропом партиций.';
CREATE INDEX ix_event_log_aggregate ON event_log (aggregate_id, created_at);
CREATE INDEX ix_event_log_type ON event_log (tenant_id, event_type, created_at DESC);
SELECT app.ensure_range_partitions('event_log', 'month', 1, 2);

-- Outbox (I-5, ADR-07): 2.5 млн строк/сутки → партиции по дням, ретеншен 7 дней.
CREATE TABLE outbox (
    id              bigint        GENERATED ALWAYS AS IDENTITY,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    event_id        uuid          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       uuid          NOT NULL,
    aggregate_type  varchar(50)   NOT NULL,
    aggregate_id    uuid          NOT NULL,
    event_type      varchar(80)   NOT NULL CHECK (event_type ~ '^[a-z_]+\.[a-z_]+\.[a-z_]+\.v[0-9]+$'),
    payload         jsonb         NOT NULL,
    correlation_id  uuid          NOT NULL,
    trace_parent    varchar(64),
    status          outbox_status NOT NULL DEFAULT 'PENDING',
    retry_count     smallint      NOT NULL DEFAULT 0,
    next_retry_at   timestamptz,
    last_error      text,
    published_at    timestamptz,
    PRIMARY KEY (created_at, id)
) PARTITION BY RANGE (created_at);
COMMENT ON TABLE outbox IS 'Событие пишется в транзакции агрегата, публикуется отдельным процессом в порядке id (порядок на агрегат гарантирован блокировкой строки агрегата). Истина о публикации — здесь, Redis — транспорт.';
CREATE INDEX ix_outbox_pending ON outbox (id) WHERE status = 'PENDING';
CREATE INDEX ix_outbox_retry ON outbox (next_retry_at) WHERE status = 'PENDING' AND next_retry_at IS NOT NULL;
CREATE INDEX ix_outbox_failed ON outbox (created_at) WHERE status = 'FAILED';
CREATE INDEX ix_outbox_aggregate ON outbox (aggregate_id, id);
SELECT app.ensure_range_partitions('outbox', 'day', 1, 3);

-- Пробуждение publisher без опроса: одно уведомление на транзакцию (pg_notify дедуплицирует одинаковые payload в TX).
CREATE OR REPLACE FUNCTION app.outbox_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('outbox_new', '');
  RETURN NULL;
END
$$;
CREATE TRIGGER trg_outbox_notify AFTER INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION app.outbox_notify();

-- Inbox потребителей (at-least-once → идемпотентность обработчиков). Партиция по created_at события: повтор того же
-- события несёт тот же created_at и попадает в ту же партицию → дедуп точен; ретеншен 7 дней дропом.
CREATE TABLE consumer_inbox (
    event_created_at  timestamptz NOT NULL,
    consumer_group    varchar(60) NOT NULL,
    event_id          uuid        NOT NULL,
    processed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_created_at, consumer_group, event_id)
) PARTITION BY RANGE (event_created_at);
SELECT app.ensure_range_partitions('consumer_inbox', 'day', 1, 3);

-- Реестр изображений: 2.5 млн/сутки, lifecycle MinIO 90 дней → партиции по месяцам, ретеншен 4 месяца.
CREATE TABLE media_object (
    object_key    text         NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    tenant_id     uuid         NOT NULL,
    lot_id        uuid         NOT NULL,
    camera_id     uuid         NOT NULL,
    event_id      uuid,
    session_id    uuid,
    status        media_status NOT NULL DEFAULT 'EXPECTED',
    content_type  varchar(40)  NOT NULL DEFAULT 'image/jpeg' CHECK (content_type = 'image/jpeg'),
    size_bytes    integer      CHECK (size_bytes IS NULL OR size_bytes BETWEEN 1 AND 8388608),
    delivered_at  timestamptz,
    deleted_at    timestamptz,
    attempts      smallint     NOT NULL DEFAULT 0,
    PRIMARY KEY (created_at, object_key),
    CONSTRAINT ck_media_key CHECK (object_key ~ '^tenant/[0-9a-f-]{36}/lot/[0-9a-f-]{36}/camera/[0-9a-f-]{36}/[0-9]{4}/[0-9]{2}/[0-9]{2}/[0-9a-f-]{36}\.jpg$')
) PARTITION BY RANGE (created_at);
COMMENT ON TABLE media_object IS 'Ключ строго по шаблону tenant/{t}/lot/{l}/camera/{c}/YYYY/MM/DD/{uuid}.jpg; принадлежность tenant/lot/camera проверяется приложением против ключа камеры (path traversal, чужие изображения).';
CREATE INDEX ix_media_undelivered ON media_object (created_at) WHERE status IN ('EXPECTED', 'UNDELIVERED');
CREATE INDEX ix_media_session ON media_object (session_id) WHERE session_id IS NOT NULL;
SELECT app.ensure_range_partitions('media_object', 'month', 1, 2);

CREATE TABLE deletion_request (
    id                        uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 uuid           NOT NULL REFERENCES tenant (id),
    state                     deletion_state NOT NULL DEFAULT 'REQUESTED',
    subject_plate_hash        bytea          NOT NULL CHECK (octet_length(subject_plate_hash) = 32),
    subject_contact_encrypted bytea,
    legal_basis               varchar(40)    NOT NULL,
    requested_at              timestamptz    NOT NULL DEFAULT now(),
    due_at                    timestamptz    NOT NULL,
    verified_at               timestamptz,
    verified_by               uuid,
    retention_note            text,
    anonymized_at             timestamptz,
    media_deleted_at          timestamptz,
    audited_at                timestamptz,
    completed_at              timestamptz,
    rejected_reason           text,
    created_by                uuid,
    created_at                timestamptz    NOT NULL DEFAULT now(),
    updated_at                timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT fk_dr_verified_by FOREIGN KEY (tenant_id, verified_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_dr_created_by FOREIGN KEY (tenant_id, created_by) REFERENCES app_user (tenant_id, id)
);
COMMENT ON TABLE deletion_request IS 'Запрос субъекта ПДн (ЗРУ-547 / GDPR). Финансовые строки не удаляются — анонимизируются; due_at — срок ответа по закону (фаза 10).';
CREATE INDEX ix_dr_open ON deletion_request (tenant_id, due_at) WHERE state NOT IN ('COMPLETED', 'REJECTED');
CREATE TRIGGER trg_dr_updated BEFORE UPDATE ON deletion_request FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
