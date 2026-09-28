REVOKE DELETE ON user_invite FROM app_maintenance;
ALTER TABLE webhook_endpoint DROP COLUMN disabled_reason, DROP COLUMN failing_since;
