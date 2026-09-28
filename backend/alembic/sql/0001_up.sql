-- 0001 Фундамент.
CREATE EXTENSION IF NOT EXISTS btree_gist;
-- pgcrypto не подключаем: gen_random_uuid() — в ядре с PG 13, HMAC/AES считаются в приложении (ключи только в памяти процесса).

CREATE SCHEMA IF NOT EXISTS app;
COMMENT ON SCHEMA app IS 'Служебные функции: tenant-контекст, триггеры-инварианты, pre-tenant lookup, управление партициями. Таблицы данных — в public.';

-- Роли приложения — объекты кластера. Создаются, если нет; downgrade их не удаляет (могут использоваться другими БД кластера).
-- Пароли выставляет оператор/ESO вне миграций (ALTER ROLE ... PASSWORD). NOBYPASSRLS — обязательно (§1.3 фазы 2).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_critical') THEN
    CREATE ROLE app_critical LOGIN NOBYPASSRLS NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_bulk') THEN
    CREATE ROLE app_bulk LOGIN NOBYPASSRLS NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_maintenance') THEN
    CREATE ROLE app_maintenance LOGIN NOBYPASSRLS NOINHERIT;
  END IF;
END
$$;
-- ADR-02: гарантия долговечности задаётся ролью, а не SET в сессии (безопасно под PgBouncer transaction pooling).
-- При пустом synchronous_standby_names (dev) remote_apply эквивалентен локальному 'on'.
ALTER ROLE app_critical SET synchronous_commit = 'remote_apply';
ALTER ROLE app_bulk SET synchronous_commit = 'off';
ALTER ROLE app_maintenance SET synchronous_commit = 'on';
ALTER ROLE app_critical SET statement_timeout = '5s';
ALTER ROLE app_critical SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE app_critical SET lock_timeout = '3s';
ALTER ROLE app_bulk SET statement_timeout = '60s';
ALTER ROLE app_bulk SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE app_maintenance SET statement_timeout = '30min';
ALTER ROLE app_maintenance SET idle_in_transaction_session_timeout = '5min';

CREATE OR REPLACE FUNCTION app.current_tenant() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
$$;
COMMENT ON FUNCTION app.current_tenant() IS 'Tenant текущей транзакции из SET LOCAL app.current_tenant_id. NULL, если не задан — политики RLS тогда не возвращают ни строки (fail-closed).';

CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

CREATE TYPE camera_direction AS ENUM ('IN', 'OUT');
CREATE TYPE camera_status AS ENUM ('UNKNOWN', 'ONLINE', 'DEGRADED', 'SILENT', 'OFFLINE');
CREATE TYPE camera_vendor AS ENUM ('GENERIC', 'HIKVISION', 'DAHUA');
CREATE TYPE barrier_state AS ENUM ('UNKNOWN', 'OPEN', 'CLOSED', 'OPENING', 'CLOSING', 'ERROR');
CREATE TYPE barrier_driver AS ENUM ('MODBUS_TCP', 'HTTP_REST');
CREATE TYPE barrier_command_kind AS ENUM ('OPEN', 'CLOSE', 'STATUS');
CREATE TYPE barrier_command_state AS ENUM ('PENDING', 'SENT', 'OK', 'FAILED', 'SKIPPED');
CREATE TYPE barrier_command_source AS ENUM ('AUTO_ENTRY', 'AUTO_EXIT', 'MANUAL', 'SYSTEM');
CREATE TYPE session_status AS ENUM ('ACTIVE', 'CLOSED', 'ORPHAN', 'MANUAL_REVIEW', 'CANCELLED');
CREATE TYPE exit_kind AS ENUM ('CAMERA', 'INFERRED_BY_PAYMENT', 'MANUAL');
CREATE TYPE anpr_event_type AS ENUM ('ENTRY', 'EXIT');
CREATE TYPE anpr_result AS ENUM ('ENTRY_CONFIRMED', 'ENTRY_DENIED', 'ENTRY_DENIED_FULL', 'MANUAL_REVIEW', 'EXIT_CONFIRMED',
  'PAYMENT_REQUIRED', 'EXIT_IN_PROGRESS', 'EXIT_WITHOUT_SESSION', 'LATE_ACCEPTED', 'REJECTED', 'QUEUED');
CREATE TYPE access_kind AS ENUM ('BLACKLIST', 'WHITELIST', 'VIP', 'SUBSCRIPTION');
CREATE TYPE payment_status AS ENUM ('PENDING', 'PAID', 'CANCELLED', 'REFUNDED', 'FAILED');
CREATE TYPE payment_method AS ENUM ('CASH', 'CARD', 'ONLINE', 'SUBSCRIPTION', 'VIP', 'WHITELIST', 'GRACE');
CREATE TYPE payment_provider AS ENUM ('NONE', 'PAYME', 'CLICK');
CREATE TYPE attempt_status AS ENUM ('PENDING', 'SUCCEEDED', 'FAILED', 'TIMEOUT', 'UNKNOWN');
CREATE TYPE refund_status AS ENUM ('PENDING', 'DONE', 'FAILED');
CREATE TYPE shift_status AS ENUM ('OPEN', 'CLOSED');
CREATE TYPE discrepancy_kind AS ENUM ('MISSING_LOCAL', 'MISSING_REMOTE', 'AMOUNT_MISMATCH', 'STATUS_MISMATCH', 'DUPLICATE_REMOTE');
CREATE TYPE discrepancy_status AS ENUM ('OPEN', 'RESOLVED', 'WRITTEN_OFF');
CREATE TYPE webhook_status AS ENUM ('NEW', 'APPLIED', 'ORPHAN', 'REJECTED', 'DUPLICATE');
CREATE TYPE saga_state AS ENUM ('ACCESS_RESOLVED', 'PAYMENT_REQUIRED', 'PAYMENT_PENDING_VERIFICATION', 'PAYMENT_SETTLED',
  'BARRIER_OPENING', 'SESSION_CLOSED', 'COMPLETED', 'MANUAL_REVIEW', 'EXPIRED', 'CANCELLED');
CREATE TYPE outbox_status AS ENUM ('PENDING', 'PUBLISHED', 'FAILED');
CREATE TYPE user_role AS ENUM ('PLATFORM_ADMIN', 'TENANT_ADMIN', 'MANAGER', 'OPERATOR', 'CASHIER', 'VIEWER');
CREATE TYPE deletion_state AS ENUM ('REQUESTED', 'IDENTITY_VERIFIED', 'LEGAL_RETENTION_CHECK', 'ANONYMIZATION',
  'MEDIA_DELETION', 'AUDIT', 'COMPLETED', 'REJECTED');
CREATE TYPE unknown_exit_policy AS ENUM ('MANUAL_REVIEW', 'OPEN_FREE');
CREATE TYPE media_status AS ENUM ('EXPECTED', 'DELIVERED', 'UNDELIVERED', 'DELETED');
CREATE TYPE actor_type AS ENUM ('USER', 'CAMERA', 'SYSTEM', 'PROVIDER');
