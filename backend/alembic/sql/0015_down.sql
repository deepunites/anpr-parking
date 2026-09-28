-- Значения enum PostgreSQL не удаляются без пересоздания типа; при откате роли остаются в типе, но код их не выдаёт.
-- Пользователи с этими ролями переводятся в TENANT_ADMIN/VIEWER, чтобы старый код их принимал.
UPDATE app_user SET role = 'TENANT_ADMIN' WHERE role IN ('OWNER', 'DEVELOPER');
UPDATE app_user SET role = 'PLATFORM_ADMIN' WHERE role = 'PLATFORM_OWNER';
