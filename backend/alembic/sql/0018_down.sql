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
DROP FUNCTION app.lookup_invite(bytea);
DROP TABLE user_invite;
DROP FUNCTION app.lookup_user_by_phone(text);
-- сотрудники только с телефоном при откате получают служебный e-mail, иначе NOT NULL не вернуть
UPDATE app_user SET email = 'phone' || substr(phone, 2) || '@no-email.invalid' WHERE email IS NULL;
ALTER TABLE app_user
  DROP CONSTRAINT ck_user_login,
  DROP CONSTRAINT uq_user_phone,
  DROP COLUMN must_change_password,
  DROP COLUMN phone,
  ALTER COLUMN email SET NOT NULL;
