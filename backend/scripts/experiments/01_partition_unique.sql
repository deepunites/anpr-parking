-- Эксперимент §1.1 / §1.2 фазы 2: partial UNIQUE и EXCLUDE на партиционированных таблицах.
\set ON_ERROR_STOP 0
SELECT version();
DROP TABLE IF EXISTS ps CASCADE;
CREATE TABLE ps (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  lot_id uuid NOT NULL, vehicle_id uuid NOT NULL, status text NOT NULL,
  PRIMARY KEY (lot_id, id)
) PARTITION BY HASH (lot_id);
CREATE TABLE ps_0 PARTITION OF ps FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE ps_1 PARTITION OF ps FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE ps_2 PARTITION OF ps FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE ps_3 PARTITION OF ps FOR VALUES WITH (MODULUS 4, REMAINDER 3);

\echo === partial UNIQUE на HASH-партиционированной таблице, ключ партиционирования включён ===
CREATE UNIQUE INDEX uq_active ON ps (lot_id, vehicle_id) WHERE status = 'ACTIVE';
\echo === принуждение ===
INSERT INTO ps (lot_id, vehicle_id, status) VALUES ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'ACTIVE');
INSERT INTO ps (lot_id, vehicle_id, status) VALUES ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'ACTIVE');
INSERT INTO ps (lot_id, vehicle_id, status) VALUES ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'CLOSED');
INSERT INTO ps (lot_id, vehicle_id, status) VALUES ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'CLOSED');
SELECT status, count(*) FROM ps GROUP BY 1 ORDER BY 1;
\echo === то же без ключа партиционирования ===
CREATE UNIQUE INDEX uq_bad ON ps (vehicle_id) WHERE status = 'ACTIVE';
\echo === EXCLUDE на партиционированной таблице ===
ALTER TABLE ps ADD CONSTRAINT ex_ps EXCLUDE USING gist (lot_id WITH =, vehicle_id WITH =) WHERE (status = 'ACTIVE');

\echo === §1.2: глобальная уникальность idempotency_key на таблице, партиционированной по месяцам ===
DROP TABLE IF EXISTS ae CASCADE;
CREATE TABLE ae (id bigint GENERATED ALWAYS AS IDENTITY, event_time timestamptz NOT NULL, idempotency_key bytea NOT NULL, PRIMARY KEY (event_time, id)) PARTITION BY RANGE (event_time);
CREATE TABLE ae_2026_09 PARTITION OF ae FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE ae_2026_10 PARTITION OF ae FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE UNIQUE INDEX uq_ae_key ON ae (idempotency_key);
CREATE UNIQUE INDEX uq_ae_key_time ON ae (idempotency_key, event_time);
INSERT INTO ae (event_time, idempotency_key) VALUES ('2026-09-30 23:59:59+05', '\x01');
INSERT INTO ae (event_time, idempotency_key) VALUES ('2026-10-01 00:00:01+05', '\x01');
SELECT count(*) AS "дубликат ключа прошёл через границу партиций" FROM ae WHERE idempotency_key = '\x01';
DROP TABLE ps CASCADE; DROP TABLE ae CASCADE;
