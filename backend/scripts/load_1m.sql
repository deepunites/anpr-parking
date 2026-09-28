-- Нагрузочные данные для EXPLAIN (§3 фазы 2): 300k vehicle, 1M parking_session, 1M anpr_event, 1M anpr_idempotency,
-- ~985k payment, 200k outbox, 100k access_rule. Требует seed_dev.sql. Время: несколько минут.
\timing on
-- повторный запуск: чистим ранее сгенерированное (сид не трогаем)
TRUNCATE parking_session, anpr_event, anpr_idempotency, payment, payment_attempt, refund, exit_saga, manual_correction, vehicle, outbox, consumer_inbox CASCADE;
DELETE FROM access_rule WHERE created_by IS NULL;
SELECT app.ensure_range_partitions('anpr_event', 'month', 3, 2);
SELECT app.ensure_range_partitions('anpr_idempotency', 'day', 62, 3);
SELECT app.ensure_range_partitions('outbox', 'day', 7, 3);
SELECT app.ensure_range_partitions('payment_attempt', 'month', 3, 2);

INSERT INTO vehicle (id, tenant_id, plate_hash, plate_encrypted, first_seen_at, last_seen_at)
SELECT gen_random_uuid(), '00000000-0000-4000-8000-0000000000a1', sha256(convert_to('A' || g, 'UTF8')), sha256(convert_to('A' || g, 'UTF8')) || sha256(convert_to('enc' || g, 'UTF8')), now() - interval '60 days', now()
FROM generate_series(1, 200000) g;
INSERT INTO vehicle (id, tenant_id, plate_hash, plate_encrypted, first_seen_at, last_seen_at)
SELECT gen_random_uuid(), '00000000-0000-4000-8000-0000000000b1', sha256(convert_to('B' || g, 'UTF8')), sha256(convert_to('B' || g, 'UTF8')) || sha256(convert_to('enc' || g, 'UTF8')), now() - interval '60 days', now()
FROM generate_series(1, 100000) g;

CREATE TEMP TABLE tmp_lots AS
SELECT lot_no, tenant_id, lot_id, cam_in, cam_out, tpv FROM (VALUES
  (0, '00000000-0000-4000-8000-0000000000a1'::uuid, '00000000-0000-4000-8000-00000000a101'::uuid, '00000000-0000-4000-8000-00000000c101'::uuid, '00000000-0000-4000-8000-00000000c102'::uuid, '00000000-0000-4000-8000-0000000a1101'::uuid),
  (1, '00000000-0000-4000-8000-0000000000a1'::uuid, '00000000-0000-4000-8000-00000000a102'::uuid, '00000000-0000-4000-8000-00000000c103'::uuid, '00000000-0000-4000-8000-00000000c104'::uuid, '00000000-0000-4000-8000-0000000a1102'::uuid),
  (2, '00000000-0000-4000-8000-0000000000b1'::uuid, '00000000-0000-4000-8000-00000000b101'::uuid, '00000000-0000-4000-8000-00000000c105'::uuid, '00000000-0000-4000-8000-00000000c106'::uuid, '00000000-0000-4000-8000-0000000b1101'::uuid)
) AS t(lot_no, tenant_id, lot_id, cam_in, cam_out, tpv);
CREATE TEMP TABLE tmp_veh AS
SELECT tenant_id, id, row_number() OVER (PARTITION BY tenant_id ORDER BY plate_hash) AS rn FROM vehicle;
CREATE INDEX ON tmp_veh (tenant_id, rn);
ALTER TABLE tmp_lots ADD COLUMN n bigint;
UPDATE tmp_lots l SET n = (SELECT count(*) FROM tmp_veh v WHERE v.tenant_id = l.tenant_id);
ANALYZE tmp_veh; ANALYZE tmp_lots;

-- 1 000 000 сессий: 985 000 CLOSED за последние 60 дней + 15 000 ACTIVE (разные машины на (lot, vehicle) — I-1).
INSERT INTO parking_session (id, tenant_id, lot_id, vehicle_id, entry_event_id, entry_camera_id, exit_camera_id, entry_time, exit_time,
                             billing_minutes, tariff_plan_version_id, status, exit_kind, version)
SELECT gen_random_uuid(), l.tenant_id, l.lot_id, v.id, gen_random_uuid(), l.cam_in,
       CASE WHEN g > 985000 THEN NULL ELSE l.cam_out END,
       ts.entry_time,
       CASE WHEN g > 985000 THEN NULL ELSE ts.entry_time + ((15 + g % 600) || ' minutes')::interval END,
       CASE WHEN g > 985000 THEN NULL ELSE 15 + g % 600 END,
       CASE WHEN g > 985000 THEN NULL ELSE l.tpv END,
       CASE WHEN g > 985000 THEN 'ACTIVE'::session_status ELSE 'CLOSED'::session_status END,
       CASE WHEN g > 985000 THEN NULL ELSE 'CAMERA'::exit_kind END,
       1
FROM generate_series(1, 1000000) AS g
JOIN tmp_lots l ON l.lot_no = g % 3
JOIN tmp_veh v ON v.tenant_id = l.tenant_id AND v.rn = 1 + (g::bigint * 7919) % l.n
CROSS JOIN LATERAL (SELECT CASE WHEN g > 985000 THEN now() - ((g % 300) || ' minutes')::interval
                                ELSE now() - interval '60 days' + ((g::bigint * 5173) % (60 * 24 * 60) || ' minutes')::interval END AS entry_time) ts;

-- события въезда: то же id, что entry_event_id
INSERT INTO anpr_event (id, event_time, tenant_id, lot_id, camera_id, event_type, direction, received_at, plate_hash, plate_encrypted,
                        confidence, image_object_key, result, session_id, idempotency_key, correlation_id)
SELECT s.entry_event_id, s.entry_time, s.tenant_id, s.lot_id, s.entry_camera_id, 'ENTRY', 'IN', s.entry_time + interval '300 ms',
       v.plate_hash, v.plate_encrypted, 80 + (abs(hashtext(s.id::text)) % 20),
       'tenant/' || s.tenant_id || '/lot/' || s.lot_id || '/camera/' || s.entry_camera_id || '/' || to_char(s.entry_time, 'YYYY/MM/DD') || '/' || s.entry_event_id || '.jpg',
       'ENTRY_CONFIRMED', s.id, sha256(convert_to(s.entry_event_id::text, 'UTF8')), gen_random_uuid()
FROM parking_session s JOIN vehicle v ON v.id = s.vehicle_id;

INSERT INTO anpr_idempotency (idempotency_key, event_second, tenant_id, camera_id, event_id, result, response)
SELECT e.idempotency_key, date_trunc('second', e.event_time), e.tenant_id, e.camera_id, e.id, e.result,
       jsonb_build_object('status', 'ENTRY_CONFIRMED', 'session_id', e.session_id)
FROM anpr_event e;

-- платежи за закрытые сессии: 60 % CASH (смена), 40 % ONLINE (Payme)
INSERT INTO payment (id, tenant_id, lot_id, session_id, amount, status, method, provider, operator_id, shift_id, calculation_snapshot,
                     idempotency_key, paid_at, created_at)
SELECT gen_random_uuid(), s.tenant_id, s.lot_id, s.id, ((s.billing_minutes + 59) / 60) * 500000,
       'PAID',
       CASE WHEN abs(hashtext(s.id::text)) % 10 < 6 THEN 'CASH'::payment_method ELSE 'ONLINE'::payment_method END,
       CASE WHEN abs(hashtext(s.id::text)) % 10 < 6 THEN 'NONE'::payment_provider ELSE 'PAYME'::payment_provider END,
       CASE WHEN abs(hashtext(s.id::text)) % 10 < 6 THEN u.user_id END,
       CASE WHEN abs(hashtext(s.id::text)) % 10 < 6 THEN u.shift_id END,
       jsonb_build_object('billing_minutes', s.billing_minutes, 'applied_rules', jsonb_build_array('HOURLY')),
       sha256(convert_to('pay-' || s.id::text, 'UTF8')), s.exit_time, s.exit_time
FROM parking_session s
JOIN (VALUES ('00000000-0000-4000-8000-0000000000a1'::uuid, '00000000-0000-4000-8000-0000000a0003'::uuid, '00000000-0000-4000-8000-000000a50001'::uuid),
             ('00000000-0000-4000-8000-0000000000b1'::uuid, '00000000-0000-4000-8000-0000000b0003'::uuid, '00000000-0000-4000-8000-000000b50001'::uuid)) AS u(tenant_id, user_id, shift_id)
  ON u.tenant_id = s.tenant_id
WHERE s.status = 'CLOSED';

-- outbox: производственный профиль после ретеншена 7 дней — 50k опубликованных в каждый из 6 прошлых дней,
-- 200k опубликованных + 100 PENDING в сегодняшней (горячей) партиции
INSERT INTO outbox (created_at, tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id, status, published_at)
SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') - (d || ' days')::interval + (g || ' seconds')::interval,
       '00000000-0000-4000-8000-0000000000a1', 'parking_session', gen_random_uuid(), 'parking.session.closed.v1',
       jsonb_build_object('n', g), gen_random_uuid(), 'PUBLISHED',
       (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') - (d || ' days')::interval + (g || ' seconds')::interval + interval '200 ms'
FROM generate_series(1, 6) d, generate_series(1, 50000) g;
INSERT INTO outbox (created_at, tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id, status, published_at)
SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') + (g % 80000 || ' seconds')::interval,
       '00000000-0000-4000-8000-0000000000a1', 'parking_session', gen_random_uuid(), 'parking.session.closed.v1',
       jsonb_build_object('n', g), gen_random_uuid(), 'PUBLISHED', now()
FROM generate_series(1, 200000) g;
INSERT INTO outbox (tenant_id, aggregate_type, aggregate_id, event_type, payload, correlation_id)
SELECT '00000000-0000-4000-8000-0000000000a1', 'parking_session', gen_random_uuid(), 'parking.session.created.v1', jsonb_build_object('n', g), gen_random_uuid()
FROM generate_series(1, 100) g;

-- правила доступа: 100k строк по случайным номерам (для §3.2–3.3)
INSERT INTO access_rule (tenant_id, lot_id, kind, plate_hash, plate_encrypted, discount_percent, validity)
SELECT '00000000-0000-4000-8000-0000000000a1',
       CASE WHEN g % 2 = 0 THEN NULL ELSE '00000000-0000-4000-8000-00000000a101'::uuid END,
       CASE g % 3 WHEN 0 THEN 'BLACKLIST'::access_kind WHEN 1 THEN 'WHITELIST'::access_kind ELSE 'VIP'::access_kind END,
       sha256(convert_to('R' || g, 'UTF8')), sha256(convert_to('R' || g, 'UTF8')) || sha256(convert_to('e', 'UTF8')),
       CASE g % 3 WHEN 0 THEN 0 WHEN 1 THEN 20 ELSE 100 END,
       tstzrange(now() - interval '100 days', now() + interval '100 days', '[)')
FROM generate_series(1, 100000) g;

DROP TABLE tmp_lots; DROP TABLE tmp_veh;
VACUUM ANALYZE;
SELECT (SELECT count(*) FROM vehicle) AS vehicles, (SELECT count(*) FROM parking_session) AS sessions,
       (SELECT count(*) FROM parking_session WHERE status = 'ACTIVE') AS active,
       (SELECT count(*) FROM anpr_event) AS events, (SELECT count(*) FROM anpr_idempotency) AS idem,
       (SELECT count(*) FROM payment) AS payments, (SELECT count(*) FROM outbox) AS outbox, (SELECT count(*) FROM access_rule) AS rules;
