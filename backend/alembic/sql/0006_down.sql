DROP TABLE IF EXISTS webhook_inbox, reconciliation_discrepancy, payment_reconciliation, refund, payment_attempt, payment, cashier_shift, provider_account CASCADE;
DROP FUNCTION IF EXISTS app.refund_settle();
DROP FUNCTION IF EXISTS app.refund_guard();
DROP FUNCTION IF EXISTS app.forbid_delete();
DROP FUNCTION IF EXISTS app.payment_guard();
DROP FUNCTION IF EXISTS app.lookup_provider_account(payment_provider, text);
