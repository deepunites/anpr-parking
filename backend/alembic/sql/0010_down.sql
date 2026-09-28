ALTER TABLE exit_saga DROP CONSTRAINT IF EXISTS fk_saga_exit_camera;
ALTER TABLE exit_saga DROP COLUMN IF EXISTS flags;
ALTER TABLE exit_saga DROP COLUMN IF EXISTS exit_camera_id;
