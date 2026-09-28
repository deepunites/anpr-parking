ALTER TABLE exit_saga ADD COLUMN exit_camera_id uuid;
ALTER TABLE exit_saga ADD COLUMN flags jsonb NOT NULL DEFAULT '{}'::jsonb;
COMMENT ON COLUMN exit_saga.flags IS 'Контекст саги: late (буферизованное событие — шлагбаум не открывать), prepaid (предоплата, A-11), manual_command_id.';
ALTER TABLE exit_saga ADD CONSTRAINT fk_saga_exit_camera FOREIGN KEY (tenant_id, lot_id, exit_camera_id) REFERENCES camera (tenant_id, lot_id, id);
