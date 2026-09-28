DROP TABLE IF EXISTS manual_correction, exit_saga, anpr_idempotency, anpr_event, parking_session, vehicle CASCADE;
DROP FUNCTION IF EXISTS app.partition_coverage(regclass);
DROP FUNCTION IF EXISTS app.drop_range_partitions(regclass, interval);
DROP FUNCTION IF EXISTS app.ensure_range_partitions(regclass, text, integer, integer);
