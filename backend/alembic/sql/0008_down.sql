DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY app.tenant_tables() || ARRAY['tenant'] LOOP
    EXECUTE format('DROP POLICY IF EXISTS rls_tenant_%s ON %I', t, t);
    EXECUTE format('DROP POLICY IF EXISTS rls_maint_%s ON %I', t, t);
    EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
  END LOOP;
END
$$;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_critical, app_bulk, app_maintenance;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_critical, app_bulk, app_maintenance;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM app_critical, app_bulk, app_maintenance;
REVOKE USAGE ON SCHEMA public, app FROM app_critical, app_bulk, app_maintenance;
DROP FUNCTION IF EXISTS app.tenant_tables();
