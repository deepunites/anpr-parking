DROP TYPE IF EXISTS actor_type, media_status, unknown_exit_policy, deletion_state, user_role, outbox_status, saga_state,
  webhook_status, discrepancy_status, discrepancy_kind, shift_status, refund_status, attempt_status, payment_provider,
  payment_method, payment_status, access_kind, anpr_result, anpr_event_type, exit_kind, session_status,
  barrier_command_source, barrier_command_state, barrier_command_kind, barrier_driver, barrier_state, camera_vendor,
  camera_status, camera_direction;
DROP FUNCTION IF EXISTS app.set_updated_at();
DROP FUNCTION IF EXISTS app.current_tenant();
DROP SCHEMA IF EXISTS app;
-- Роли app_* намеренно не удаляются (кластерные объекты).
DROP EXTENSION IF EXISTS btree_gist;
