DROP TABLE IF EXISTS access_rule, subscription, tariff_plan_version, holiday, holiday_calendar, tariff_plan CASCADE;
DROP FUNCTION IF EXISTS app.tariff_rules_valid(jsonb);
