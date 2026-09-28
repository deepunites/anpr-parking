-- v2 этап 1: роли OWNER (владелец парковки), DEVELOPER (интеграции/устройства), PLATFORM_OWNER (владелец платформы).
-- Матрица прав — в коде (identity/domain/permissions.py); БД знает только имена ролей.
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'OWNER';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'DEVELOPER';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'PLATFORM_OWNER';
