-- откат: профили снимаются с партиций, функция ensure_range_partitions возвращается к версии 0005 (без профиля)
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.oid FROM app.autovacuum_profile p JOIN pg_inherits i ON i.inhparent = p.parent JOIN pg_class c ON c.oid = i.inhrelid LOOP
    EXECUTE format('ALTER TABLE %s RESET (autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold, autovacuum_analyze_scale_factor, autovacuum_vacuum_cost_delay, autovacuum_vacuum_cost_limit, fillfactor)', r.oid::regclass);
  END LOOP;
END $$;
ALTER TABLE webhook_inbox RESET (autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold, autovacuum_analyze_scale_factor);
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
  RETURN v_created;
END
$$;
DROP FUNCTION app.apply_autovacuum_profile(regclass);
DROP TABLE app.autovacuum_profile;
