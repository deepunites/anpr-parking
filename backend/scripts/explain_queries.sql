-- EXPLAIN (ANALYZE, BUFFERS) для 9 запросов §3 фазы 2 на данных load_1m.sql. Требование: Index Scan, Seq Scan на горячем пути — провал.
\set tenant '00000000-0000-4000-8000-0000000000a1'
\set lot    '00000000-0000-4000-8000-00000000a101'
-- 30 000 камер для запроса 9 (на 6 строках любой планировщик выберет Seq Scan)
INSERT INTO camera (tenant_id, lot_id, code, name, direction, last_heartbeat_at)
SELECT :'tenant', '00000000-0000-4000-8000-00000000a102', 'CAM_' || g, 'cam ' || g, 'IN',
       CASE WHEN g % 100 = 0 THEN now() - interval '1 hour' ELSE now() - (g % 200 || ' seconds')::interval END
FROM generate_series(1, 30000) g ON CONFLICT DO NOTHING;
ANALYZE camera;

SELECT v.plate_hash AS ph FROM parking_session s JOIN vehicle v ON v.id = s.vehicle_id
 WHERE s.status = 'ACTIVE' AND s.lot_id = :'lot' LIMIT 1 \gset
SELECT event_second AS sec, idempotency_key AS ik FROM anpr_idempotency WHERE tenant_id = :'tenant' LIMIT 1 \gset
SELECT entry_time AS cursor_t, id AS cursor_id FROM parking_session WHERE lot_id = :'lot' ORDER BY entry_time DESC, id DESC OFFSET 1000 LIMIT 1 \gset
SELECT sha256(convert_to('R3', 'UTF8')) AS rph \gset

\echo
\echo '=== 1. Активная сессия по (tenant_id, lot_id, plate_hash) — горячий путь EXIT ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT s.id, s.entry_time
FROM vehicle v
JOIN parking_session s ON s.vehicle_id = v.id AND s.lot_id = :'lot' AND s.status = 'ACTIVE'
WHERE v.tenant_id = :'tenant' AND v.plate_hash = :'ph'::bytea AND s.tenant_id = :'tenant';

\echo
\echo '=== 2. Blacklist по (tenant_id, plate_hash), lot_id IS NULL как глобальное правило ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, lot_id, validity FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'rph'::bytea AND kind = 'BLACKLIST'
  AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL;

\echo
\echo '=== 3. Все виды правил (whitelist, VIP, subscription) одним запросом с проверкой срока ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT kind, lot_id, discount_percent, source_id FROM access_rule
WHERE tenant_id = :'tenant' AND plate_hash = :'rph'::bytea
  AND (lot_id IS NULL OR lot_id = :'lot') AND validity @> now() AND revoked_at IS NULL;

\echo
\echo '=== 4. Идемпотентность по ключу (с секундой события → одна партиция) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT response FROM anpr_idempotency WHERE event_second = :'sec' AND idempotency_key = :'ik'::bytea;

\echo
\echo '=== 5. Outbox: неопубликованные с FOR UPDATE SKIP LOCKED ==='
BEGIN;
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, event_type FROM outbox
WHERE status = 'PENDING' AND (next_retry_at IS NULL OR next_retry_at <= now())
ORDER BY id LIMIT 500 FOR UPDATE SKIP LOCKED;
ROLLBACK;

\echo
\echo '=== 6. Occupancy: число активных сессий площадки ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM parking_session WHERE lot_id = :'lot' AND status = 'ACTIVE';

\echo
\echo '=== 7. Дневной отчёт: платежи tenant за диапазон дат по способу оплаты ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT method, count(*), sum(amount) FROM payment
WHERE tenant_id = :'tenant' AND status = 'PAID'
  AND paid_at >= now() - interval '30 days' AND paid_at < now() - interval '29 days'
GROUP BY method;

\echo
\echo '=== 8. Лента оператора: 50 сессий площадки после курсора (entry_time, id) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, entry_time, status FROM parking_session
WHERE lot_id = :'lot' AND (entry_time, id) < (:'cursor_t'::timestamptz, :'cursor_id'::uuid)
ORDER BY entry_time DESC, id DESC LIMIT 50;

\echo
\echo '=== 9. Камеры без heartbeat дольше порога ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, lot_id, last_heartbeat_at FROM camera
WHERE active AND (last_heartbeat_at IS NULL OR last_heartbeat_at < now() - interval '10 minutes');
