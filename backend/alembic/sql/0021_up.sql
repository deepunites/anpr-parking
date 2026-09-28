-- v2 этап 7 «Дашборды»: история статусов камер (доступность устройств за период).
-- Пишется триггером на camera.status — любой путь смены статуса (монитор здоровья, отключение) попадает в историю.
CREATE TABLE camera_status_log (
    id          bigint         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid           NOT NULL,
    lot_id      uuid           NOT NULL,
    camera_id   uuid           NOT NULL,
    status      camera_status  NOT NULL,
    changed_at  timestamptz    NOT NULL DEFAULT now()
);
COMMENT ON TABLE camera_status_log IS 'Переходы статуса камер для отчёта о доступности (дашборд владельца). Хранится 400 дней (retention.sql).';
CREATE INDEX ix_camera_status_log ON camera_status_log (tenant_id, camera_id, changed_at);

CREATE OR REPLACE FUNCTION app.log_camera_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
    INSERT INTO camera_status_log (tenant_id, lot_id, camera_id, status) VALUES (NEW.tenant_id, NEW.lot_id, NEW.id, NEW.status);
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_camera_status_log AFTER INSERT OR UPDATE OF status ON camera
  FOR EACH ROW EXECUTE FUNCTION app.log_camera_status();

-- отправная точка истории: текущий статус каждой камеры
INSERT INTO camera_status_log (tenant_id, lot_id, camera_id, status, changed_at)
SELECT tenant_id, lot_id, id, status, now() FROM camera;

ALTER TABLE camera_status_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_camera_status_log ON camera_status_log AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_maint_camera_status_log ON camera_status_log AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON camera_status_log TO app_critical, app_maintenance;
GRANT SELECT, INSERT ON camera_status_log TO app_bulk;  -- монитор здоровья пишет статусы камер пулом bulk
GRANT DELETE ON camera_status_log TO app_maintenance;

-- отчёты: ручные действия и смены за период
CREATE INDEX ix_shift_tenant_closed ON cashier_shift (tenant_id, closed_at) WHERE status = 'CLOSED';
CREATE INDEX ix_refund_report ON refund (tenant_id, completed_at) WHERE status = 'DONE';

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
    'api_key', 'webhook_endpoint', 'webhook_delivery', 'event_journal', 'camera_status_log']
$$;
