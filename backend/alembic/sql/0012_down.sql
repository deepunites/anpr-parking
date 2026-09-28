ALTER TABLE refund DROP CONSTRAINT IF EXISTS ck_refund_operator;
-- Возвраты, инициированные провайдером, оператора не имеют: при откате они приписываются старейшему пользователю
-- tenant с пометкой в reason — данные не удаляются (I-6), а факт отката виден.
UPDATE refund r SET operator_id = (SELECT u.id FROM app_user u WHERE u.tenant_id = r.tenant_id ORDER BY u.created_at LIMIT 1),
                    reason = '[downgrade 0012: provider-initiated, operator unknown] ' || r.reason
WHERE r.operator_id IS NULL;
ALTER TABLE refund ALTER COLUMN operator_id SET NOT NULL;
DROP INDEX IF EXISTS ix_payment_prepaid_open;
DROP INDEX IF EXISTS ix_refund_pending_provider;
DROP INDEX IF EXISTS ix_attempt_unknown;
ALTER TABLE webhook_inbox DROP COLUMN response;
