-- 0013 Фаза 9 §3: отдельный autovacuum-профиль для таблиц с высоким оборотом. Партиционированные родители не
-- передают reloptions партициям, поэтому профиль (а) применяется к существующим листовым партициям и (б) наследуется
-- новыми партициями через app.ensure_range_partitions (таблица app.autovacuum_profile).
--
-- Почему: anpr_idempotency (INSERT на каждое событие + TTL), outbox (INSERT + UPDATE status/published_at на каждое
-- событие = мёртвая строка на событие), consumer_inbox/webhook_inbox (INSERT + удаление по ретеншену) раздуваются
-- быстрее всех; дефолт autovacuum (scale_factor 0.2 = «после 20 % мёртвых строк») на партиции дня с миллионами
-- строк означает вакуум раз в сутки и bloat ×1.2–1.5. Профиль: порог 2 % + 1000 строк, analyze 1 %, дешёвая задержка.
CREATE TABLE IF NOT EXISTS app.autovacuum_profile (
  parent   regclass PRIMARY KEY,
  options  text NOT NULL   -- reloptions через запятую, как в WITH (...)
);
COMMENT ON TABLE app.autovacuum_profile IS 'Autovacuum-профили горячих партиционированных таблиц (фаза 9 §3); применяются ко всем листовым партициям';

INSERT INTO app.autovacuum_profile(parent, options) VALUES
  ('anpr_idempotency', 'autovacuum_vacuum_scale_factor=0.02, autovacuum_vacuum_threshold=1000, autovacuum_analyze_scale_factor=0.01, autovacuum_vacuum_cost_delay=1, autovacuum_vacuum_cost_limit=2000'),
  ('outbox',           'autovacuum_vacuum_scale_factor=0.02, autovacuum_vacuum_threshold=1000, autovacuum_analyze_scale_factor=0.01, autovacuum_vacuum_cost_delay=1, autovacuum_vacuum_cost_limit=2000, fillfactor=80'),
  ('consumer_inbox',   'autovacuum_vacuum_scale_factor=0.05, autovacuum_vacuum_threshold=1000, autovacuum_analyze_scale_factor=0.02'),
  ('anpr_event',       'autovacuum_vacuum_scale_factor=0.05, autovacuum_vacuum_threshold=5000, autovacuum_analyze_scale_factor=0.02')
ON CONFLICT (parent) DO UPDATE SET options = EXCLUDED.options;

-- webhook_inbox — обычная таблица: профиль напрямую
ALTER TABLE webhook_inbox SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_threshold = 500, autovacuum_analyze_scale_factor = 0.02);

CREATE OR REPLACE FUNCTION app.apply_autovacuum_profile(p_parent regclass)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_options text;
  r record;
  v_applied integer := 0;
BEGIN
  SELECT options INTO v_options FROM app.autovacuum_profile WHERE parent = p_parent;
  IF v_options IS NULL THEN RETURN 0; END IF;
  FOR r IN SELECT c.oid FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = p_parent LOOP
    EXECUTE format('ALTER TABLE %s SET (%s)', r.oid::regclass, v_options);
    v_applied := v_applied + 1;
  END LOOP;
  RETURN v_applied;
END
$$;
COMMENT ON FUNCTION app.apply_autovacuum_profile(regclass) IS 'ALTER TABLE ... SET (autovacuum_*) на все листовые партиции по app.autovacuum_profile';

-- ensure_range_partitions: новые партиции сразу получают профиль (тело функции 0005 + вызов apply_autovacuum_profile)
CREATE OR REPLACE FUNCTION app.ensure_range_partitions(p_parent regclass, p_unit text, p_behind integer, p_ahead integer)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_step    interval;
  v_fmt     text;
  v_schema  text;
  v_parent  text;
  v_start   timestamptz;
  v_from    timestamptz;
  v_to      timestamptz;
  v_name    text;
  v_created integer := 0;
  i         integer;
BEGIN
  IF p_unit = 'day' THEN v_step := interval '1 day'; v_fmt := 'YYYYMMDD';
  ELSIF p_unit = 'month' THEN v_step := interval '1 month'; v_fmt := 'YYYYMM';
  ELSE RAISE EXCEPTION 'ensure_range_partitions: unit must be day|month, got %', p_unit;
  END IF;
  SELECT n.nspname, c.relname INTO v_schema, v_parent
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = p_parent;
  v_start := (date_trunc(p_unit, now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') - p_behind * v_step;
  FOR i IN 0 .. p_behind + p_ahead LOOP
    v_from := v_start + i * v_step;
    v_to := v_from + v_step;
    v_name := v_parent || '_' || to_char(v_from AT TIME ZONE 'UTC', v_fmt);
    IF to_regclass(format('%I.%I', v_schema, v_name)) IS NULL THEN
      EXECUTE format('CREATE TABLE %I.%I PARTITION OF %I.%I FOR VALUES FROM (%L) TO (%L)',
                     v_schema, v_name, v_schema, v_parent, v_from, v_to);
      v_created := v_created + 1;
    END IF;
  END LOOP;
  IF v_created > 0 THEN
    PERFORM app.apply_autovacuum_profile(p_parent);
  END IF;
  RETURN v_created;
END
$$;

SELECT app.apply_autovacuum_profile(parent) FROM app.autovacuum_profile;
