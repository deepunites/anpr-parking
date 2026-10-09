-- Шлагбаум через реле самой ANPR-камеры Hikvision (DS-TCG406-E и аналоги): команда ISAPI вместо отдельного реле.
-- Логин/пароль камеры хранятся зашифрованными ключом клиента (AES-256-GCM, как секреты вебхуков), в API не отдаются.
ALTER TYPE barrier_driver ADD VALUE IF NOT EXISTS 'HIKVISION_ISAPI';
ALTER TABLE barrier ADD COLUMN credentials_encrypted bytea;
COMMENT ON COLUMN barrier.credentials_encrypted IS 'Учётные данные контроллера (HIKVISION_ISAPI: логин/пароль камеры), AES-256-GCM DEK клиента; AAD = tenant + версия + barrier:<id>.';
