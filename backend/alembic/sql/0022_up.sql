-- v2 этап 8: адрес вебхука, не отвечающий 3 суток подряд, выключается автоматически (риск плана «ретраи копятся»).
ALTER TABLE webhook_endpoint
  ADD COLUMN failing_since   timestamptz,
  ADD COLUMN disabled_reason varchar(40);
COMMENT ON COLUMN webhook_endpoint.failing_since IS 'Первая неудача после последней успешной доставки; NULL — адрес отвечает.';
COMMENT ON COLUMN webhook_endpoint.disabled_reason IS 'AUTO_FAILING_3D — выключен диспетчером после 3 суток отказов; включение вручную сбрасывает.';

-- PRIVACY §1 (этап 8): контакты из приглашений не копятся — retention удаляет приглашения через 30 дней после срока.
GRANT DELETE ON user_invite TO app_maintenance;
