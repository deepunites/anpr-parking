-- 0008 RLS + GRANT.
-- Правило (фаза 2 §1.3): RLS — второй рубеж после обязательного фильтра tenant_id в репозиториях; проверяются оба независимо.
-- app.current_tenant() = NULL, если SET LOCAL не выполнен → ни одна строка не видна и не записывается (fail-closed).
-- Партиции прав не получают: доступ только через родителя, у которого действует политика.

CREATE OR REPLACE FUNCTION app.tenant_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY[
    'parking_lot', 'parking_spot', 'app_user', 'user_lot', 'refresh_token',
    'camera', 'camera_api_key', 'barrier', 'barrier_command',
    'tariff_plan', 'holiday_calendar', 'holiday', 'tariff_plan_version', 'subscription', 'access_rule',
    'vehicle', 'parking_session', 'anpr_event', 'anpr_idempotency', 'exit_saga', 'manual_correction',
    'provider_account', 'cashier_shift', 'payment', 'payment_attempt', 'refund',
    'payment_reconciliation', 'reconciliation_discrepancy', 'webhook_inbox',
    'audit_log', 'event_log', 'outbox', 'media_object', 'deletion_request']
$$;
COMMENT ON FUNCTION app.tenant_tables() IS 'Список tenant-owned таблиц. Тест I-2 обходит его и проверяет, что у каждой таблицы с колонкой tenant_id включён RLS.';

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY app.tenant_tables() LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY rls_tenant_%s ON %I AS PERMISSIVE FOR ALL TO app_critical, app_bulk USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant())', t, t);
    EXECUTE format('CREATE POLICY rls_maint_%s ON %I AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true)', t, t);
  END LOOP;
END
$$;
ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_tenant ON tenant FOR ALL TO app_critical, app_bulk USING (id = app.current_tenant()) WITH CHECK (id = app.current_tenant());
CREATE POLICY rls_maint_tenant ON tenant FOR ALL TO app_maintenance USING (true) WITH CHECK (true);

GRANT USAGE ON SCHEMA public, app TO app_critical, app_bulk, app_maintenance;
GRANT EXECUTE ON FUNCTION app.current_tenant() TO app_critical, app_bulk, app_maintenance;
GRANT EXECUTE ON FUNCTION app.lookup_user_by_email(text), app.lookup_camera_keys(text),
  app.lookup_provider_account(payment_provider, text) TO app_critical;
GRANT EXECUTE ON FUNCTION app.ensure_range_partitions(regclass, text, integer, integer),
  app.drop_range_partitions(regclass, interval), app.partition_coverage(regclass) TO app_maintenance;

-- app_critical: рабочие таблицы; append-only таблицы — только INSERT/SELECT.
GRANT SELECT, INSERT, UPDATE ON tenant, parking_lot, parking_spot, app_user, user_lot, refresh_token,
  camera, camera_api_key, barrier, barrier_command, tariff_plan, holiday_calendar, subscription, access_rule,
  vehicle, parking_session, exit_saga, provider_account, cashier_shift, payment, payment_attempt, refund,
  payment_reconciliation, reconciliation_discrepancy, webhook_inbox, deletion_request, media_object TO app_critical;
GRANT SELECT, INSERT, UPDATE, DELETE ON holiday TO app_critical;
GRANT SELECT, INSERT ON tariff_plan_version, anpr_event, anpr_idempotency, manual_correction, outbox,
  consumer_inbox, audit_log, event_log, login_attempt TO app_critical;

-- app_bulk: журналы, проекции, статусы устройств (в пределах tenant-контекста).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_bulk;
GRANT INSERT ON event_log, media_object, consumer_inbox TO app_bulk;
GRANT UPDATE (status, last_heartbeat_at, last_event_at, updated_at) ON camera TO app_bulk;
GRANT UPDATE (state, last_heartbeat_at, updated_at) ON barrier TO app_bulk;
GRANT UPDATE (last_used_at) ON camera_api_key TO app_bulk;
GRANT UPDATE (status, delivered_at, deleted_at, attempts, size_bytes) ON media_object TO app_bulk;

-- app_maintenance: кросс-tenant джобы — publisher, ретеншен, флаш heartbeat, поиск просроченных саг/сессий.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_maintenance;
GRANT UPDATE ON outbox, camera, barrier, camera_api_key, media_object TO app_maintenance;
GRANT DELETE ON refresh_token, login_attempt TO app_maintenance;
GRANT INSERT ON consumer_inbox, event_log TO app_maintenance;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_critical, app_bulk, app_maintenance;
REVOKE ALL ON alembic_version FROM app_critical, app_bulk, app_maintenance;
-- I-11 / I-16 / I-6: ни UPDATE, ни DELETE, ни TRUNCATE у ролей приложения на audit_log, anpr_event, manual_correction, tariff_plan_version,
-- payment_attempt-history не выдавались выше; убеждаемся явно.
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log, event_log, anpr_event, manual_correction, tariff_plan_version, anpr_idempotency
  FROM app_critical, app_bulk, app_maintenance;
