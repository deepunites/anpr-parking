-- Сквозная нумерация чеков (MediaPark, 2026-10-19: «в чеке нет номера»): реестр выданных чеков площадки.
-- Оплата после PAID неизменяема (I-6) — номер живёт здесь, а не в payment. Номер выдаётся при первой печати чека
-- оплаченного платежа (повторная печать — тот же номер) или при продаже абонемента; счёт до оплаты номера не получает.
-- Счётчик — parking_lot.receipt_seq: UPDATE … RETURNING под блокировкой строки площадки, номера без пропусков.
ALTER TABLE parking_lot ADD COLUMN receipt_seq bigint NOT NULL DEFAULT 0 CHECK (receipt_seq >= 0);

-- Оплата по QR, не выходя из машины (MediaPark, 2026-10-19): строка статичного QR банка площадки (UzQR/EMVCo) —
-- печатается на «Счёте к оплате»; деньги приходят на счёт площадки, кассир подтверждает «Оплачено по QR».
ALTER TABLE parking_lot ADD COLUMN payment_qr varchar(512);
ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'QR';

CREATE TABLE receipt (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid        NOT NULL,
    lot_id          uuid        NOT NULL,
    receipt_no      bigint      NOT NULL CHECK (receipt_no > 0),
    payment_id      uuid,
    subscription_id uuid,
    amount          bigint      NOT NULL CHECK (amount >= 0),
    issued_by       uuid,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_receipt_source CHECK ((payment_id IS NULL) <> (subscription_id IS NULL)),
    CONSTRAINT uq_receipt_no UNIQUE (tenant_id, lot_id, receipt_no),
    CONSTRAINT uq_receipt_payment UNIQUE (tenant_id, payment_id),
    CONSTRAINT uq_receipt_subscription UNIQUE (tenant_id, subscription_id),
    CONSTRAINT fk_receipt_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_receipt_payment FOREIGN KEY (tenant_id, payment_id) REFERENCES payment (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_receipt_subscription FOREIGN KEY (tenant_id, subscription_id) REFERENCES subscription (tenant_id, id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_receipt_issued_by FOREIGN KEY (tenant_id, issued_by) REFERENCES app_user (tenant_id, id)
);
COMMENT ON TABLE receipt IS 'Выданные бумажные чеки: номер по площадке без пропусков, что оплачено (стоянка или абонемент), кто выдал. Только добавление.';
CREATE TRIGGER trg_receipt_no_delete BEFORE DELETE ON receipt FOR EACH ROW EXECUTE FUNCTION app.forbid_delete();

ALTER TABLE receipt ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_receipt ON receipt AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_maint_receipt ON receipt AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON receipt TO app_critical, app_maintenance;

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
    'api_key', 'webhook_endpoint', 'webhook_delivery', 'event_journal', 'camera_status_log', 'receipt']
$$;
