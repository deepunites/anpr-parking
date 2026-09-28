-- Фаза 3.4: обоснование ADR-03 — одна таблица правил (один запрос) против четырёх таблиц (UNION ALL / 4 запроса) на 1 млн строк.
-- Довод до 1 000 000 правил (load_1m.sql даёт 100 000) и замер обоих вариантов на прогретом кеше.
\timing on
\set tenant '00000000-0000-4000-8000-0000000000a1'
\set lot    '00000000-0000-4000-8000-00000000a101'
INSERT INTO access_rule (tenant_id, lot_id, kind, plate_hash, plate_encrypted, discount_percent, validity)
SELECT :'tenant',
       CASE WHEN g % 2 = 0 THEN NULL ELSE :'lot'::uuid END,
       CASE g % 4 WHEN 0 THEN 'BLACKLIST'::access_kind WHEN 1 THEN 'WHITELIST'::access_kind WHEN 2 THEN 'VIP'::access_kind ELSE 'WHITELIST'::access_kind END,
       sha256(convert_to('B' || g, 'UTF8')), sha256(convert_to('B' || g, 'UTF8')) || sha256(convert_to('e', 'UTF8')),
       CASE g % 4 WHEN 0 THEN 0 WHEN 1 THEN 20 WHEN 2 THEN 100 ELSE 50 END,
       tstzrange(now() - interval '100 days', now() + interval '100 days', '[)')
FROM generate_series(1, 900000) g
ON CONFLICT DO NOTHING;
VACUUM ANALYZE access_rule;
SELECT count(*) AS access_rules FROM access_rule;
SELECT sha256(convert_to('B424242', 'UTF8')) AS ph \gset

\echo '=== вариант A: единая таблица, один запрос (ADR-03) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, kind, lot_id, validity, discount_percent, source_id FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'ph'::bytea AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL;

\echo '=== вариант B: четыре запроса по видам (эквивалент UNION ALL по четырём таблицам с тем же индексом) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, kind, lot_id, validity, discount_percent, source_id FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'ph'::bytea AND kind = 'BLACKLIST' AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL
UNION ALL
SELECT id, kind, lot_id, validity, discount_percent, source_id FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'ph'::bytea AND kind = 'VIP' AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL
UNION ALL
SELECT id, kind, lot_id, validity, discount_percent, source_id FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'ph'::bytea AND kind = 'SUBSCRIPTION' AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL
UNION ALL
SELECT id, kind, lot_id, validity, discount_percent, source_id FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'ph'::bytea AND kind = 'WHITELIST' AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL;

\echo '=== 1000 повторов каждого варианта, суммарное время ==='
DO $$
DECLARE t0 timestamptz; i int; r record;
BEGIN
  t0 := clock_timestamp();
  FOR i IN 1..1000 LOOP
    PERFORM id FROM access_rule WHERE tenant_id = '00000000-0000-4000-8000-0000000000a1' AND plate_hash = sha256(convert_to('B' || (i * 97 % 900000 + 1), 'UTF8'))
      AND (lot_id IS NULL OR lot_id = '00000000-0000-4000-8000-00000000a101') AND validity @> now() AND revoked_at IS NULL;
  END LOOP;
  RAISE NOTICE 'A: единая таблица, 1000 запросов: % ms', round(extract(epoch FROM clock_timestamp() - t0) * 1000);
  t0 := clock_timestamp();
  FOR i IN 1..1000 LOOP
    PERFORM id FROM (
      SELECT id FROM access_rule WHERE tenant_id = '00000000-0000-4000-8000-0000000000a1' AND plate_hash = sha256(convert_to('B' || (i * 97 % 900000 + 1), 'UTF8')) AND kind = 'BLACKLIST' AND (lot_id IS NULL OR lot_id = '00000000-0000-4000-8000-00000000a101') AND validity @> now() AND revoked_at IS NULL
      UNION ALL SELECT id FROM access_rule WHERE tenant_id = '00000000-0000-4000-8000-0000000000a1' AND plate_hash = sha256(convert_to('B' || (i * 97 % 900000 + 1), 'UTF8')) AND kind = 'VIP' AND (lot_id IS NULL OR lot_id = '00000000-0000-4000-8000-00000000a101') AND validity @> now() AND revoked_at IS NULL
      UNION ALL SELECT id FROM access_rule WHERE tenant_id = '00000000-0000-4000-8000-0000000000a1' AND plate_hash = sha256(convert_to('B' || (i * 97 % 900000 + 1), 'UTF8')) AND kind = 'SUBSCRIPTION' AND (lot_id IS NULL OR lot_id = '00000000-0000-4000-8000-00000000a101') AND validity @> now() AND revoked_at IS NULL
      UNION ALL SELECT id FROM access_rule WHERE tenant_id = '00000000-0000-4000-8000-0000000000a1' AND plate_hash = sha256(convert_to('B' || (i * 97 % 900000 + 1), 'UTF8')) AND kind = 'WHITELIST' AND (lot_id IS NULL OR lot_id = '00000000-0000-4000-8000-00000000a101') AND validity @> now() AND revoked_at IS NULL
    ) u;
  END LOOP;
  RAISE NOTICE 'B: четыре подзапроса (UNION ALL), 1000 запросов: % ms', round(extract(epoch FROM clock_timestamp() - t0) * 1000);
END $$;
