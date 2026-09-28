DROP TABLE IF EXISTS deletion_request, media_object, consumer_inbox, outbox, event_log, audit_log CASCADE;
DROP FUNCTION IF EXISTS app.outbox_notify();
