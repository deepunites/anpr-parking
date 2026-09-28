-- v2: карточка машины (марка, модель, цвет) и ручная регистрация въезда оператором.
-- Марка/модель/цвет — не ПДн сами по себе, но привязаны к номеру: при стирании данных субъекта очищаются вместе с номером.
ALTER TABLE vehicle
  ADD COLUMN make  varchar(40),
  ADD COLUMN model varchar(60),
  ADD COLUMN color varchar(20),
  ADD COLUMN attributes_updated_at timestamptz;
COMMENT ON COLUMN vehicle.make IS 'Марка (Chevrolet, Lada…), вводится оператором; очищается при анонимизации';

ALTER TABLE parking_session ADD COLUMN entry_manual boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN parking_session.entry_manual IS 'Въезд зарегистрирован оператором вручную (камера не видела машину); reason — в audit_log MANUAL_ENTRY (I-15)';
ALTER TABLE parking_session DROP CONSTRAINT ck_entry_known;
ALTER TABLE parking_session ADD CONSTRAINT ck_entry_known
  CHECK (entry_unknown OR entry_inferred OR entry_manual OR entry_camera_id IS NOT NULL);

CREATE OR REPLACE FUNCTION app.anonymize_vehicles(
  p_tenant_id uuid, p_vehicle_ids uuid[], p_token bytea, p_sentinel bytea, p_key_version smallint, p_now timestamptz
) RETURNS TABLE (vehicles integer, events integer, corrections integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_ctx text := current_setting('app.current_tenant_id', true);
  v_hashes bytea[];
  v_vehicles integer; v_events integer; v_corrections integer;
BEGIN
  IF v_ctx IS NULL OR v_ctx <> p_tenant_id::text THEN
    RAISE EXCEPTION 'anonymize_vehicles: tenant context % does not match %', v_ctx, p_tenant_id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF octet_length(p_token) <> 32 THEN
    RAISE EXCEPTION 'anonymize_vehicles: token must be 32 bytes';
  END IF;
  SELECT array_agg(plate_hash) INTO v_hashes FROM vehicle WHERE tenant_id = p_tenant_id AND id = ANY (p_vehicle_ids);
  UPDATE anpr_event SET plate_hash = p_token, plate_encrypted = p_sentinel, enc_key_version = p_key_version
   WHERE tenant_id = p_tenant_id AND plate_hash = ANY (coalesce(v_hashes, '{}'));
  GET DIAGNOSTICS v_events = ROW_COUNT;
  UPDATE manual_correction SET corrected_plate_encrypted = p_sentinel, enc_key_version = p_key_version
   WHERE tenant_id = p_tenant_id AND (new_vehicle_id = ANY (p_vehicle_ids) OR old_vehicle_id = ANY (p_vehicle_ids));
  GET DIAGNOSTICS v_corrections = ROW_COUNT;
  UPDATE vehicle SET plate_hash = p_token, plate_encrypted = p_sentinel, enc_key_version = p_key_version,
                     anonymized_at = p_now, plate_format = NULL, make = NULL, model = NULL, color = NULL,
                     attributes_updated_at = p_now
   WHERE tenant_id = p_tenant_id AND id = ANY (p_vehicle_ids) AND anonymized_at IS NULL;
  GET DIAGNOSTICS v_vehicles = ROW_COUNT;
  RETURN QUERY SELECT v_vehicles, v_events, v_corrections;
END
$$;
