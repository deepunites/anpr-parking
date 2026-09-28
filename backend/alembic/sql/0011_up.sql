-- Способ оплаты определяется фактом оплаты (наличные у кассира или онлайн), а не в момент создания обязательства.
ALTER TABLE payment ALTER COLUMN method DROP NOT NULL;
ALTER TABLE payment ADD CONSTRAINT ck_method_known_when_settled CHECK (status = 'PENDING' OR status = 'CANCELLED' OR method IS NOT NULL);
ALTER TABLE payment DROP CONSTRAINT ck_provider_methods;
ALTER TABLE payment ADD CONSTRAINT ck_provider_methods CHECK (method IS NULL OR ((method IN ('CARD', 'ONLINE')) = (provider <> 'NONE')));
