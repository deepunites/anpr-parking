-- v2 этап 3 «Продажа»: тарифы платформы, статус подписки клиента, ключи клиентов в конверте (envelope encryption).
-- Самостоятельная регистрация клиента требует ключей шифрования номеров «на лету»: DEK/HMAC генерируются при
-- регистрации и хранятся здесь обёрнутыми ключом платформы (KEK из Vault/окружения). В БД — только шифртекст:
-- дамп без KEK номеров не раскрывает (как и раньше — THREAT_MODEL T-17).

CREATE TABLE platform_plan (
    code            varchar(20)  PRIMARY KEY,
    name            varchar(60)  NOT NULL,
    max_lots        integer      CHECK (max_lots IS NULL OR max_lots > 0),
    max_places      integer      CHECK (max_places IS NULL OR max_places > 0),
    max_cameras     integer      CHECK (max_cameras IS NULL OR max_cameras > 0),
    price_minor     bigint       CHECK (price_minor IS NULL OR price_minor >= 0),
    trial_days      integer      NOT NULL DEFAULT 14 CHECK (trial_days >= 0),
    sort_order      integer      NOT NULL DEFAULT 0,
    active          boolean      NOT NULL DEFAULT true
);
COMMENT ON TABLE platform_plan IS 'Тарифы платформы (подписка клиента). NULL в лимите — без ограничения; price_minor NULL — по договору (A-77).';
INSERT INTO platform_plan (code, name, max_lots, max_places, max_cameras, price_minor, trial_days, sort_order) VALUES
  ('START',    'Старт',  1,    100,  2,    49000000,  14, 1),
  ('BUSINESS', 'Бизнес', 3,    500,  8,    149000000, 14, 2),
  ('NETWORK',  'Сеть',   NULL, NULL, NULL, NULL,      14, 3);

ALTER TABLE tenant
  ADD COLUMN plan_code       varchar(20) NOT NULL DEFAULT 'NETWORK' REFERENCES platform_plan (code),
  ADD COLUMN status          varchar(12) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('TRIAL', 'ACTIVE', 'SUSPENDED')),
  ADD COLUMN trial_ends_at   timestamptz,
  ADD COLUMN paid_until      timestamptz,
  ADD COLUMN contact_phone   varchar(30),
  ADD COLUMN signup_source   varchar(20);
COMMENT ON COLUMN tenant.status IS 'TRIAL — пробный период до trial_ends_at; ACTIVE — оплачен (paid_until); SUSPENDED — выключен владельцем платформы. Ограничивает только настройку (площадки, камеры), не проезд машин.';

CREATE TABLE tenant_key (
    tenant_id       uuid        NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    version         smallint    NOT NULL CHECK (version > 0),
    wrapped_hmac    bytea       NOT NULL CHECK (octet_length(wrapped_hmac) = 60),
    wrapped_dek     bytea       NOT NULL CHECK (octet_length(wrapped_dek) = 60),
    kek_id          varchar(40) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, version)
);
COMMENT ON TABLE tenant_key IS 'DEK/HMAC клиента, обёрнутые KEK платформы (AES-256-GCM: nonce 12 + ключ 32 + тег 16 = 60 байт; AAD = tenant:version:purpose). Только INSERT: ротация — новая версия.';
ALTER TABLE tenant_key ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_tenant_key ON tenant_key AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_maint_tenant_key ON tenant_key AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON tenant_key TO app_critical;
GRANT SELECT ON tenant_key TO app_maintenance, app_bulk;
GRANT SELECT ON platform_plan TO app_critical, app_bulk, app_maintenance;
-- владелец платформы меняет статус/тариф/оплату клиента через свой контекст tenant (app_critical), список — через maintenance
GRANT UPDATE ON tenant TO app_maintenance;

CREATE OR REPLACE FUNCTION app.tenant_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY[
    'parking_lot', 'parking_spot', 'app_user', 'user_lot', 'refresh_token',
    'camera', 'camera_api_key', 'barrier', 'barrier_command',
    'tariff_plan', 'holiday_calendar', 'holiday', 'tariff_plan_version', 'subscription', 'access_rule',
    'vehicle', 'parking_session', 'anpr_event', 'anpr_idempotency', 'exit_saga', 'manual_correction',
    'provider_account', 'cashier_shift', 'payment', 'payment_attempt', 'refund',
    'payment_reconciliation', 'reconciliation_discrepancy', 'webhook_inbox',
    'audit_log', 'event_log', 'outbox', 'media_object', 'deletion_request', 'tenant_key']
$$;
