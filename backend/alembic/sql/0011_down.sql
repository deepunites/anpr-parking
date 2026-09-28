ALTER TABLE payment DROP CONSTRAINT IF EXISTS ck_provider_methods;
ALTER TABLE payment ADD CONSTRAINT ck_provider_methods CHECK ((method IN ('CARD', 'ONLINE')) = (provider <> 'NONE'));
ALTER TABLE payment DROP CONSTRAINT IF EXISTS ck_method_known_when_settled;
-- Схема до 0011 не умеет хранить обязательство без способа оплаты (CASH требует кассира и смену, ONLINE — провайдера):
-- откат возможен только когда таких строк нет — они либо оплачены, либо отменены сагой. Явный отказ вместо подделки данных.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM payment WHERE method IS NULL) THEN
    RAISE EXCEPTION 'downgrade 0011: payments without method exist (PENDING/CANCELLED obligations) — settle or cancel them and set a method first';
  END IF;
END $$;
ALTER TABLE payment ALTER COLUMN method SET NOT NULL;
