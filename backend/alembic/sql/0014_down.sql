DROP FUNCTION IF EXISTS app.anonymize_vehicles(uuid, uuid[], bytea, bytea, smallint, timestamptz);
ALTER TABLE deletion_request DROP COLUMN vehicle_ids;
