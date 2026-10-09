-- Значение enum HIKVISION_ISAPI остаётся: PostgreSQL не удаляет значения enum; шлагбаумы этого типа выключаются.
UPDATE barrier SET active = false WHERE driver = 'HIKVISION_ISAPI';
ALTER TABLE barrier DROP COLUMN credentials_encrypted;
