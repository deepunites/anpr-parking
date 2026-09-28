-- 0012 Billing (фаза 5): ответ вебхука хранится для идемпотентного повтора; индексы джобов платежей.
ALTER TABLE webhook_inbox ADD COLUMN response jsonb;
COMMENT ON COLUMN webhook_inbox.response IS 'Ответ, отданный провайдеру при первой обработке: тот же вебхук дважды = тот же ответ и один эффект.';
CREATE INDEX ix_attempt_unknown ON payment_attempt (started_at) WHERE status IN ('UNKNOWN', 'TIMEOUT');
CREATE INDEX ix_refund_pending_provider ON refund (created_at) WHERE status = 'PENDING' AND method IN ('CARD', 'ONLINE');
CREATE INDEX ix_payment_prepaid_open ON payment (paid_until) WHERE status = 'PAID' AND paid_until IS NOT NULL;
-- Возврат, инициированный провайдером (CancelTransaction после оплаты, сверка): оператора нет — компенсирующая
-- запись всё равно обязана существовать (I-6: платёж не мутируется, REFUNDED — производно).
ALTER TABLE refund ALTER COLUMN operator_id DROP NOT NULL;
ALTER TABLE refund ADD CONSTRAINT ck_refund_operator CHECK (operator_id IS NOT NULL OR method IN ('CARD', 'ONLINE'));
