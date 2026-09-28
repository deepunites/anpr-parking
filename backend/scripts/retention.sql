-- Джоб партиций и ретеншена (DATABASE.md §3). Выполняется ролью app_maintenance ежечасно (Celery maintenance — фаза 3);
-- идемпотентен, возвращает счётчики для метрик partition_created_total / partition_dropped_total.
SELECT 'anpr_event'        AS parent, app.ensure_range_partitions('anpr_event',        'month', 1, 2)  AS created, app.drop_range_partitions('anpr_event',        interval '400 days') AS dropped
UNION ALL SELECT 'anpr_idempotency', app.ensure_range_partitions('anpr_idempotency', 'day',   2, 3),  app.drop_range_partitions('anpr_idempotency', interval '7 days')
UNION ALL SELECT 'outbox',           app.ensure_range_partitions('outbox',           'day',   1, 3),  app.drop_range_partitions('outbox',           interval '7 days')
UNION ALL SELECT 'consumer_inbox',   app.ensure_range_partitions('consumer_inbox',   'day',   1, 3),  app.drop_range_partitions('consumer_inbox',   interval '7 days')
UNION ALL SELECT 'barrier_command',  app.ensure_range_partitions('barrier_command',  'month', 1, 2),  app.drop_range_partitions('barrier_command',  interval '13 months')
UNION ALL SELECT 'media_object',     app.ensure_range_partitions('media_object',     'month', 1, 2),  app.drop_range_partitions('media_object',     interval '4 months')
UNION ALL SELECT 'audit_log',        app.ensure_range_partitions('audit_log',        'month', 1, 2),  app.drop_range_partitions('audit_log',        interval '5 years')
UNION ALL SELECT 'event_log',        app.ensure_range_partitions('event_log',        'month', 1, 2),  app.drop_range_partitions('event_log',        interval '3 years')
UNION ALL SELECT 'event_journal',    app.ensure_range_partitions('event_journal',    'day',   1, 3),  app.drop_range_partitions('event_journal',    interval '7 days')
UNION ALL SELECT 'payment_attempt',  app.ensure_range_partitions('payment_attempt',  'month', 1, 2),  0;  -- финансовые: не дропаются
SELECT parent, app.partition_coverage(parent::regclass) AS coverage_ahead
FROM unnest(ARRAY['anpr_event', 'anpr_idempotency', 'outbox', 'consumer_inbox', 'barrier_command', 'media_object', 'audit_log', 'event_log', 'payment_attempt']) AS parent;
-- v2 этап 6: журнал доставок вебхуков — 30 дней (не партиционирован: объём мал)
DELETE FROM webhook_delivery WHERE created_at < now() - interval '30 days';
-- v2 этап 7: история статусов камер — 400 дней; последний известный статус камеры сохраняется всегда
DELETE FROM camera_status_log l WHERE l.changed_at < now() - interval '400 days'
  AND EXISTS (SELECT 1 FROM camera_status_log n WHERE n.camera_id = l.camera_id AND n.changed_at > l.changed_at);
-- v2 этап 8 (PRIVACY): приглашения сотрудников (e-mail/телефон) — 30 дней после истечения срока ссылки
DELETE FROM user_invite WHERE expires_at < now() - interval '30 days';
