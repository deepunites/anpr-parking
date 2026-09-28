-- v2 этап 6 «Интеграции»: API-ключи внешних систем, исходящие вебхуки с журналом доставок, журнал событий камер.

-- API-ключ: в БД — префикс (поиск) и HMAC полного ключа с pepper (как ключи камер); сам ключ виден один раз.
CREATE TABLE api_key (
    id            uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid         NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    name          varchar(80)  NOT NULL,
    prefix        varchar(16)  NOT NULL CHECK (length(prefix) = 16),
    key_hmac      bytea        NOT NULL CHECK (octet_length(key_hmac) = 32),
    scopes        text[]       NOT NULL CHECK (cardinality(scopes) > 0),
    lot_scope     uuid[]       NOT NULL DEFAULT '{}',
    expires_at    timestamptz,
    created_by    uuid         NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    revoked_by    uuid,
    CONSTRAINT uq_api_key_prefix UNIQUE (prefix)
);
COMMENT ON TABLE api_key IS 'Ключи внешних систем: права — подмножество прав создателя (scopes), срок, площадки. Отзыв — revoked_at.';
CREATE INDEX ix_api_key_tenant ON api_key (tenant_id, created_at DESC);

CREATE OR REPLACE FUNCTION app.lookup_api_key(p_prefix text)
RETURNS TABLE (id uuid, tenant_id uuid, key_hmac bytea, scopes text[], lot_scope uuid[], expires_at timestamptz,
               revoked_at timestamptz, name varchar, tenant_active boolean, created_by uuid, creator_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT k.id, k.tenant_id, k.key_hmac, k.scopes, k.lot_scope, k.expires_at, k.revoked_at, k.name, t.active,
         k.created_by, coalesce(u.active, false)
  FROM api_key k JOIN tenant t ON t.id = k.tenant_id
  LEFT JOIN app_user u ON u.tenant_id = k.tenant_id AND u.id = k.created_by
  WHERE k.prefix = p_prefix
$$;
REVOKE ALL ON FUNCTION app.lookup_api_key(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.lookup_api_key(text) TO app_critical;

-- Вебхуки: секрет подписи зашифрован ключом клиента (DEK, AES-GCM); доставки — очередь с повторами и DLQ.
CREATE TABLE webhook_endpoint (
    id                    uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid          NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    url                   varchar(500)  NOT NULL,
    description           varchar(200),
    events                text[]        NOT NULL CHECK (cardinality(events) > 0),
    secret_encrypted      bytea         NOT NULL,
    secret_hint           varchar(12)   NOT NULL,
    active                boolean       NOT NULL DEFAULT true,
    consecutive_failures  integer       NOT NULL DEFAULT 0,
    created_by            uuid          NOT NULL,
    created_at            timestamptz   NOT NULL DEFAULT now(),
    updated_at            timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX ix_webhook_endpoint_tenant ON webhook_endpoint (tenant_id);

CREATE TABLE webhook_delivery (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid         NOT NULL,
    endpoint_id      uuid         NOT NULL REFERENCES webhook_endpoint (id) ON DELETE CASCADE,
    event_type       varchar(60)  NOT NULL,
    event_id         uuid         NOT NULL,
    payload          jsonb        NOT NULL,
    status           varchar(12)  NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'RETRY', 'DELIVERED', 'DEAD')),
    attempts         smallint     NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz,
    last_status      smallint,
    last_error       varchar(300),
    last_latency_ms  integer,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    delivered_at     timestamptz,
    CONSTRAINT uq_delivery_event UNIQUE (endpoint_id, event_id)
);
COMMENT ON TABLE webhook_delivery IS 'Доставки вебхуков: попытка сразу, повторы через 1, 5 и 30 минут, затем DEAD (DLQ, повтор вручную). Хранятся 30 дней.';
CREATE INDEX ix_delivery_due ON webhook_delivery (next_attempt_at) WHERE status IN ('PENDING', 'RETRY');
CREATE INDEX ix_delivery_endpoint ON webhook_delivery (endpoint_id, created_at DESC);

-- Журнал событий камер (после проверки ключа): принятые, дубли, отклонённые с причиной; номер — только маской.
CREATE TABLE event_journal (
    id              uuid          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       uuid          NOT NULL,
    lot_id          uuid,
    camera_id       uuid,
    source          varchar(20)   NOT NULL,
    received_at     timestamptz   NOT NULL DEFAULT now(),
    outcome         varchar(12)   NOT NULL CHECK (outcome IN ('ACCEPTED', 'DUPLICATE', 'QUEUED', 'REJECTED', 'ERROR')),
    status          varchar(40),
    http_status     smallint,
    event_type      varchar(10),
    plate_masked    varchar(20) CHECK (plate_masked IS NULL OR position('*' in plate_masked) > 0),  -- I-10: только маска
    message         varchar(300),
    correlation_id  uuid,
    latency_ms      integer,
    PRIMARY KEY (received_at, id)
) PARTITION BY RANGE (received_at);
COMMENT ON TABLE event_journal IS 'Журнал запросов камер за 7 дней (партиции по дням, retention.sql): что пришло, что ответили и почему отклонили.';
CREATE INDEX ix_journal_tenant ON event_journal (tenant_id, received_at DESC);
CREATE INDEX ix_journal_correlation ON event_journal (correlation_id);
SELECT app.ensure_range_partitions('event_journal', 'day', 1, 3);

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['api_key', 'webhook_endpoint', 'webhook_delivery', 'event_journal'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY rls_tenant_%s ON %I AS PERMISSIVE FOR ALL TO app_critical, app_bulk USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant())', t, t);
    EXECUTE format('CREATE POLICY rls_maint_%s ON %I AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true)', t, t);
  END LOOP;
END
$$;
GRANT SELECT, INSERT, UPDATE ON api_key, webhook_endpoint, webhook_delivery TO app_critical;
GRANT DELETE ON webhook_endpoint TO app_critical;
GRANT SELECT ON api_key, webhook_endpoint, webhook_delivery, event_journal TO app_bulk, app_maintenance;
GRANT SELECT ON event_journal TO app_critical;
GRANT INSERT ON event_journal TO app_bulk;
-- диспетчер доставок работает поверх всех клиентов (как publisher outbox): выбирает и обновляет доставки
GRANT UPDATE ON webhook_delivery, webhook_endpoint TO app_maintenance;
GRANT DELETE ON webhook_delivery TO app_maintenance;

CREATE OR REPLACE FUNCTION app.tenant_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY[
    'parking_lot', 'parking_spot', 'app_user', 'user_lot', 'refresh_token',
    'camera', 'camera_api_key', 'barrier', 'barrier_command',
    'tariff_plan', 'holiday_calendar', 'holiday', 'tariff_plan_version', 'subscription', 'access_rule',
    'vehicle', 'parking_session', 'anpr_event', 'anpr_idempotency', 'exit_saga', 'manual_correction',
    'provider_account', 'cashier_shift', 'payment', 'payment_attempt', 'refund',
    'payment_reconciliation', 'reconciliation_discrepancy', 'webhook_inbox',
    'audit_log', 'event_log', 'outbox', 'media_object', 'deletion_request', 'tenant_key', 'user_invite', 'lot_layout', 'api_key', 'webhook_endpoint', 'webhook_delivery', 'event_journal']
$$;
