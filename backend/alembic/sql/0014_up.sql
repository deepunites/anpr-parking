-- 0014 Фаза 10 §3: машины субъекта фиксируются в запросе на удаление — после анонимизации по хешу их уже не найти,
-- а удаление изображений (следующий шаг) и повторные прогоны должны знать, чьи ключи удалять.
ALTER TABLE deletion_request ADD COLUMN vehicle_ids uuid[] NOT NULL DEFAULT '{}';
COMMENT ON COLUMN deletion_request.vehicle_ids IS 'vehicle.id субъекта, найденные на шаге LEGAL_RETENTION_CHECK (по ним идут ANONYMIZATION и MEDIA_DELETION)';

-- Анонимизация — единственный разрешённый UPDATE append-only таблиц anpr_event/manual_correction (I-11/I-16 остаются
-- в силе для ролей: у app_critical нет UPDATE на эти таблицы, есть только EXECUTE на функцию). SECURITY DEFINER
-- обходит RLS, поэтому tenant передаётся явно и сверяется с контекстом сессии.
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
  -- события: тот же необратимый токен вместо хеша, заглушка вместо шифртекста (номер не восстановим)
  UPDATE anpr_event SET plate_hash = p_token, plate_encrypted = p_sentinel, enc_key_version = p_key_version
   WHERE tenant_id = p_tenant_id AND plate_hash = ANY (coalesce(v_hashes, '{}'));
  GET DIAGNOSTICS v_events = ROW_COUNT;
  UPDATE manual_correction SET corrected_plate_encrypted = p_sentinel, enc_key_version = p_key_version
   WHERE tenant_id = p_tenant_id AND (new_vehicle_id = ANY (p_vehicle_ids) OR old_vehicle_id = ANY (p_vehicle_ids));
  GET DIAGNOSTICS v_corrections = ROW_COUNT;
  UPDATE vehicle SET plate_hash = p_token, plate_encrypted = p_sentinel, enc_key_version = p_key_version,
                     anonymized_at = p_now, plate_format = NULL
   WHERE tenant_id = p_tenant_id AND id = ANY (p_vehicle_ids) AND anonymized_at IS NULL;
  GET DIAGNOSTICS v_vehicles = ROW_COUNT;
  RETURN QUERY SELECT v_vehicles, v_events, v_corrections;
END
$$;
REVOKE ALL ON FUNCTION app.anonymize_vehicles(uuid, uuid[], bytea, bytea, smallint, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.anonymize_vehicles(uuid, uuid[], bytea, bytea, smallint, timestamptz) TO app_critical;
COMMENT ON FUNCTION app.anonymize_vehicles(uuid, uuid[], bytea, bytea, smallint, timestamptz) IS
  'Privacy (фаза 10 §3): необратимая замена номера токеном во vehicle/anpr_event/manual_correction; финансовые строки не трогает';
