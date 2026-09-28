-- v2 этап 5 «Карта»: схема площадки версиями (зоны, ряды мест, проезды, устройства, подписи) и координаты мест.
-- Документ схемы — jsonb, проверяется приложением (tenancy/domain/layout.py); каждая правка — новая версия,
-- прежние остаются для истории и отката. Места из рядов схемы синхронизируются в parking_spot (код, зона, x, y).

CREATE TABLE lot_layout (
    tenant_id   uuid         NOT NULL,
    lot_id      uuid         NOT NULL,
    version     integer      NOT NULL CHECK (version > 0),
    document    jsonb        NOT NULL,
    spots       integer      NOT NULL DEFAULT 0 CHECK (spots >= 0),
    created_by  uuid,
    reason      varchar(500),
    created_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (lot_id, version),
    CONSTRAINT fk_layout_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE CASCADE
);
COMMENT ON TABLE lot_layout IS 'Версии схемы площадки. Текущая — максимальная version; правка с устаревшей base_version отклоняется (409 LAYOUT_CONFLICT).';
ALTER TABLE lot_layout ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_lot_layout ON lot_layout AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_maint_lot_layout ON lot_layout AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON lot_layout TO app_critical;
GRANT SELECT ON lot_layout TO app_bulk, app_maintenance;

ALTER TABLE parking_spot
  ADD COLUMN x integer,
  ADD COLUMN y integer;
COMMENT ON COLUMN parking_spot.x IS 'Координаты места на схеме площадки (lot_layout), NULL — место заведено без схемы.';

CREATE OR REPLACE FUNCTION app.tenant_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY[
    'parking_lot', 'parking_spot', 'app_user', 'user_lot', 'refresh_token',
    'camera', 'camera_api_key', 'barrier', 'barrier_command',
    'tariff_plan', 'holiday_calendar', 'holiday', 'tariff_plan_version', 'subscription', 'access_rule',
    'vehicle', 'parking_session', 'anpr_event', 'anpr_idempotency', 'exit_saga', 'manual_correction',
    'provider_account', 'cashier_shift', 'payment', 'payment_attempt', 'refund',
    'payment_reconciliation', 'reconciliation_discrepancy', 'webhook_inbox',
    'audit_log', 'event_log', 'outbox', 'media_object', 'deletion_request', 'tenant_key', 'user_invite', 'lot_layout']
$$;
