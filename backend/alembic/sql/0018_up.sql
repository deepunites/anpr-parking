-- v2 этап 4 «Сотрудники»: телефон как второй логин, временный пароль со сменой при первом входе, приглашения по ссылке.

-- Оператор без почты входит по телефону: e-mail становится необязательным, но хотя бы один логин есть всегда.
ALTER TABLE app_user
  ALTER COLUMN email DROP NOT NULL,
  ADD COLUMN phone varchar(16) CHECK (phone ~ '^\+[0-9]{9,15}$'),
  ADD COLUMN must_change_password boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT uq_user_phone UNIQUE (phone),
  ADD CONSTRAINT ck_user_login CHECK (email IS NOT NULL OR phone IS NOT NULL);
COMMENT ON COLUMN app_user.phone IS 'E.164 (+998901234567). Глобально уникален, как e-mail: логин без указания клиента.';
COMMENT ON COLUMN app_user.must_change_password IS 'Пароль выдан администратором (создание, сброс): до смены доступны только /auth/me, /auth/password, выход.';

-- Pre-tenant поиск по телефону — единственный путь чтения app_user без tenant-контекста (как lookup_user_by_email)
CREATE OR REPLACE FUNCTION app.lookup_user_by_phone(p_phone text)
RETURNS TABLE (id uuid, tenant_id uuid, password_hash text, role user_role, token_version integer, active boolean,
               failed_logins smallint, locked_until timestamptz, tenant_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT u.id, u.tenant_id, u.password_hash, u.role, u.token_version, u.active, u.failed_logins, u.locked_until, t.active
  FROM app_user u JOIN tenant t ON t.id = u.tenant_id
  WHERE u.phone = p_phone
$$;
REVOKE ALL ON FUNCTION app.lookup_user_by_phone(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.lookup_user_by_phone(text) TO app_critical;

CREATE TABLE user_invite (
    id                uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid         NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    token_hash        bytea        NOT NULL CHECK (octet_length(token_hash) = 32),
    email             varchar(254) CHECK (email = lower(email) AND position('@' in email) > 1),
    phone             varchar(16)  CHECK (phone ~ '^\+[0-9]{9,15}$'),
    role              user_role    NOT NULL,
    display_name      varchar(100) NOT NULL,
    lot_scope         uuid[]       NOT NULL DEFAULT '{}',
    created_by        uuid         NOT NULL,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    expires_at        timestamptz  NOT NULL,
    accepted_at       timestamptz,
    accepted_user_id  uuid,
    revoked_at        timestamptz,
    CONSTRAINT uq_invite_token UNIQUE (token_hash),
    CONSTRAINT ck_invite_contact CHECK (email IS NOT NULL OR phone IS NOT NULL),
    CONSTRAINT ck_invite_final CHECK (accepted_at IS NULL OR revoked_at IS NULL)
);
COMMENT ON TABLE user_invite IS 'Приглашение сотрудника по ссылке (48 ч). В БД — только sha256 токена; сам токен видит администратор один раз и отправляет каналом D-3 (A-76).';
CREATE INDEX ix_invite_tenant ON user_invite (tenant_id, created_at DESC);
ALTER TABLE user_invite ENABLE ROW LEVEL SECURITY;
CREATE POLICY rls_tenant_user_invite ON user_invite AS PERMISSIVE FOR ALL TO app_critical, app_bulk
  USING (tenant_id = app.current_tenant()) WITH CHECK (tenant_id = app.current_tenant());
CREATE POLICY rls_maint_user_invite ON user_invite AS PERMISSIVE FOR ALL TO app_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON user_invite TO app_critical;
GRANT SELECT ON user_invite TO app_bulk, app_maintenance;

-- Открытие ссылки приглашения: по хешу токена узнаём только клиента, дальше — обычный tenant-контекст
CREATE OR REPLACE FUNCTION app.lookup_invite(p_token_hash bytea)
RETURNS TABLE (id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT i.id, i.tenant_id FROM user_invite i WHERE i.token_hash = p_token_hash
$$;
REVOKE ALL ON FUNCTION app.lookup_invite(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.lookup_invite(bytea) TO app_critical;

CREATE OR REPLACE FUNCTION app.tenant_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY[
    'parking_lot', 'parking_spot', 'app_user', 'user_lot', 'refresh_token',
    'camera', 'camera_api_key', 'barrier', 'barrier_command',
    'tariff_plan', 'holiday_calendar', 'holiday', 'tariff_plan_version', 'subscription', 'access_rule',
    'vehicle', 'parking_session', 'anpr_event', 'anpr_idempotency', 'exit_saga', 'manual_correction',
    'provider_account', 'cashier_shift', 'payment', 'payment_attempt', 'refund',
    'payment_reconciliation', 'reconciliation_discrepancy', 'webhook_inbox',
    'audit_log', 'event_log', 'outbox', 'media_object', 'deletion_request', 'tenant_key', 'user_invite']
$$;
