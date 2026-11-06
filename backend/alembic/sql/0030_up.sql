-- Партнёры площадки (MediaPark, 2026-11-06): компьютерный клуб выпускает своих гостей — бармен вводит номер машины,
-- на выезде стрела открывается сама, без оплаты; клуб платит площадке фиксированную сумму в месяц (monthly_fee),
-- при желании — ещё и за каждую отмеченную машину (price_per_car, по умолчанию 0).
CREATE TABLE partner (
    id             uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid         NOT NULL,
    lot_id         uuid         NOT NULL,
    name           varchar(100) NOT NULL CHECK (length(btrim(name)) > 0),
    price_per_car  bigint       NOT NULL DEFAULT 0 CHECK (price_per_car >= 0),
    monthly_fee    bigint       NOT NULL DEFAULT 0 CHECK (monthly_fee >= 0),
    active         boolean      NOT NULL DEFAULT true,
    created_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_partner_tenant UNIQUE (tenant_id, id),
    CONSTRAINT fk_partner_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT
);
COMMENT ON TABLE partner IS 'Партнёр площадки (клуб, кафе): его сотрудники отмечают машины гостей — выезд бесплатно; партнёр платит площадке monthly_fee в месяц (+ price_per_car за машину, если задана).';
CREATE TRIGGER trg_partner_updated BEFORE UPDATE ON partner FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- кто из сотрудников отмечает машины за партнёра (роль PARTNER: видит только поиск машины по номеру и свои отметки)
CREATE TABLE partner_member (
    tenant_id   uuid        NOT NULL,
    partner_id  uuid        NOT NULL,
    user_id     uuid        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    CONSTRAINT fk_pm_partner FOREIGN KEY (tenant_id, partner_id) REFERENCES partner (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_pm_user FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id) ON DELETE CASCADE
);

-- отметка «выезд за счёт партнёра»: цена — на момент отметки (отчёт месяца не меняется от новой цены)
CREATE TABLE partner_validation (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid        NOT NULL,
    lot_id         uuid        NOT NULL,
    partner_id     uuid        NOT NULL,
    session_id     uuid        NOT NULL,
    price_per_car  bigint      NOT NULL CHECK (price_per_car >= 0),
    created_by     uuid        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    cancelled_at   timestamptz,
    cancelled_by   uuid,
    cancel_reason  text,
    CONSTRAINT uq_pv_tenant UNIQUE (tenant_id, id),
    CONSTRAINT ck_pv_cancel CHECK (cancelled_at IS NULL OR (cancelled_by IS NOT NULL AND length(btrim(cancel_reason)) >= 3)),
    CONSTRAINT fk_pv_partner FOREIGN KEY (tenant_id, partner_id) REFERENCES partner (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_pv_session FOREIGN KEY (tenant_id, lot_id, session_id) REFERENCES parking_session (tenant_id, lot_id, id),
    CONSTRAINT fk_pv_created_by FOREIGN KEY (tenant_id, created_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_pv_cancelled_by FOREIGN KEY (tenant_id, cancelled_by) REFERENCES app_user (tenant_id, id)
);
CREATE UNIQUE INDEX uq_pv_session_active ON partner_validation (session_id) WHERE cancelled_at IS NULL;
CREATE INDEX ix_pv_partner_period ON partner_validation (tenant_id, partner_id, created_at);
CREATE TRIGGER trg_pv_no_delete BEFORE DELETE ON partner_validation FOR EACH ROW EXECUTE FUNCTION app.forbid_delete();

-- сессия знает свою отметку: выезд читает её вместе с сессией, без лишнего запроса
ALTER TABLE parking_session ADD COLUMN partner_validation_id uuid;

ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'PARTNER';
ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'PARTNER';

ALTER TABLE partner ENABLE ROW LEVEL SECURITY;
ALTER TABLE partner_member ENABLE ROW LEVEL SECURITY;
ALTER TABLE partner_validation ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_partner ON partner AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_tenant_partner_member ON partner_member AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_tenant_partner_validation ON partner_validation AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_maint_partner ON partner AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
CREATE POLICY rls_maint_partner_member ON partner_member AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
CREATE POLICY rls_maint_partner_validation ON partner_validation AS PERMISSIVE FOR ALL TO app_maintenance
  USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON partner TO app_critical, app_maintenance;
GRANT SELECT, INSERT, UPDATE, DELETE ON partner_member TO app_critical, app_maintenance;
GRANT SELECT, INSERT, UPDATE ON partner_validation TO app_critical, app_maintenance;

CREATE OR REPLACE FUNCTION app.tenant_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY[
    'parking_lot', 'parking_spot', 'app_user', 'user_lot', 'refresh_token',
    'camera', 'camera_api_key', 'barrier', 'barrier_command',
    'tariff_plan', 'holiday_calendar', 'holiday', 'tariff_plan_version', 'subscription', 'access_rule',
    'vehicle', 'parking_session', 'anpr_event', 'anpr_idempotency', 'exit_saga', 'manual_correction',
    'provider_account', 'cashier_shift', 'payment', 'payment_attempt', 'refund',
    'payment_reconciliation', 'reconciliation_discrepancy', 'webhook_inbox',
    'audit_log', 'event_log', 'outbox', 'media_object', 'deletion_request', 'tenant_key', 'user_invite', 'lot_layout',
    'api_key', 'webhook_endpoint', 'webhook_delivery', 'event_journal', 'camera_status_log', 'receipt',
    'partner', 'partner_member', 'partner_validation']
$$;
