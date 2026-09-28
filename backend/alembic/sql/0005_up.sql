-- 0005 Parking + ANPR.

-- ---------- управление RANGE-партициями (используется отсюда и далее) ----------
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
  -- границы в UTC, независимо от timezone сессии
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
COMMENT ON FUNCTION app.ensure_range_partitions(regclass, text, integer, integer) IS
  'Создаёт партиции от now()-behind до now()+ahead (UTC). Идемпотентна. Вызывается миграцией и джобом maintenance (ежечасно).';

CREATE OR REPLACE FUNCTION app.drop_range_partitions(p_parent regclass, p_retain interval)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  r         record;
  v_upper   timestamptz;
  v_dropped integer := 0;
BEGIN
  FOR r IN
    SELECT c.oid, c.relname, pg_get_expr(c.relpartbound, c.oid) AS bound
    FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
    WHERE i.inhparent = p_parent
  LOOP
    v_upper := (regexp_match(r.bound, 'TO \(''([^'']+)''\)'))[1]::timestamptz;
    IF v_upper <= now() - p_retain THEN
      EXECUTE format('DROP TABLE %s', r.oid::regclass);
      RAISE NOTICE 'dropped partition % (upper bound %)', r.relname, v_upper;
      v_dropped := v_dropped + 1;
    END IF;
  END LOOP;
  RETURN v_dropped;
END
$$;
COMMENT ON FUNCTION app.drop_range_partitions(regclass, interval) IS
  'Ретеншен через DROP партиций, чья верхняя граница старше now()-retain. Никаких DELETE (CORE §14).';

CREATE OR REPLACE FUNCTION app.partition_coverage(p_parent regclass)
RETURNS interval
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT max((regexp_match(pg_get_expr(c.relpartbound, c.oid), 'TO \(''([^'']+)''\)'))[1]::timestamptz) - now()
  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
  WHERE i.inhparent = p_parent
$$;
COMMENT ON FUNCTION app.partition_coverage(regclass) IS 'Сколько времени вперёд покрыто партициями — метрика partition_coverage_seconds, алерт при < 24 ч.';
-- SECURITY DEFINER + EXECUTE по умолчанию у PUBLIC = любой логин мог бы создавать/дропать партиции. Только app_maintenance (0008).
REVOKE ALL ON FUNCTION app.ensure_range_partitions(regclass, text, integer, integer), app.drop_range_partitions(regclass, interval),
  app.partition_coverage(regclass) FROM PUBLIC;

-- barrier_command (0003) создана до функций партиций — первые партиции создаём здесь
SELECT app.ensure_range_partitions('barrier_command', 'month', 1, 2);

-- ---------- Vehicle (ADR-06) ----------
CREATE TABLE vehicle (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid        NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    plate_hash        bytea       NOT NULL CHECK (octet_length(plate_hash) = 32),
    plate_encrypted   bytea       NOT NULL CHECK (octet_length(plate_encrypted) >= 28),
    hmac_key_version  smallint    NOT NULL DEFAULT 1,
    enc_key_version   smallint    NOT NULL DEFAULT 1,
    country_code      char(2),
    plate_format      varchar(20),
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    anonymized_at     timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_vehicle_plate  UNIQUE (tenant_id, plate_hash),
    CONSTRAINT uq_vehicle_tenant UNIQUE (tenant_id, id)
);
COMMENT ON TABLE vehicle IS 'Идентичность номера в пределах tenant. Открытого номера нет ни в одной колонке (I-10): plate_hash = HMAC-SHA256(ключ tenant, нормализованный номер), plate_encrypted = AES-256-GCM nonce||ct||tag.';
COMMENT ON COLUMN vehicle.anonymized_at IS 'Privacy: после анонимизации plate_hash заменён необратимым токеном, plate_encrypted — пустым шифртекстом; финансовые связи сохранены.';
CREATE TRIGGER trg_vehicle_updated BEFORE UPDATE ON vehicle FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------- Parking session: HASH по lot_id, 32 партиции (ADR-08) ----------
CREATE TABLE parking_session (
    id                      uuid            NOT NULL DEFAULT gen_random_uuid(),
    tenant_id               uuid            NOT NULL,
    lot_id                  uuid            NOT NULL,
    vehicle_id              uuid            NOT NULL,
    entry_event_id          uuid,
    exit_event_id           uuid,
    entry_camera_id         uuid,
    exit_camera_id          uuid,
    entry_time              timestamptz     NOT NULL,
    exit_time               timestamptz,
    billing_minutes         integer         CHECK (billing_minutes IS NULL OR billing_minutes >= 0),
    tariff_plan_version_id  uuid,
    status                  session_status  NOT NULL DEFAULT 'ACTIVE',
    exit_kind               exit_kind,
    entry_access_kind       varchar(12)     NOT NULL DEFAULT 'NORMAL',
    entry_unknown           boolean         NOT NULL DEFAULT false,
    entry_inferred          boolean         NOT NULL DEFAULT false,
    review_reason           varchar(40),
    closed_reason           varchar(40),
    version                 integer         NOT NULL DEFAULT 1,
    created_at              timestamptz     NOT NULL DEFAULT now(),
    updated_at              timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (lot_id, id),
    CONSTRAINT uq_session_tenant_lot UNIQUE (tenant_id, lot_id, id),
    CONSTRAINT fk_session_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_session_vehicle FOREIGN KEY (tenant_id, vehicle_id) REFERENCES vehicle (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_session_entry_camera FOREIGN KEY (tenant_id, lot_id, entry_camera_id) REFERENCES camera (tenant_id, lot_id, id),
    CONSTRAINT fk_session_exit_camera FOREIGN KEY (tenant_id, lot_id, exit_camera_id) REFERENCES camera (tenant_id, lot_id, id),
    CONSTRAINT fk_session_tariff FOREIGN KEY (tenant_id, tariff_plan_version_id) REFERENCES tariff_plan_version (tenant_id, id),
    CONSTRAINT ck_exit_after_entry CHECK (exit_time IS NULL OR exit_time >= entry_time),
    CONSTRAINT ck_closed_complete CHECK (status <> 'CLOSED' OR (exit_time IS NOT NULL AND billing_minutes IS NOT NULL AND exit_kind IS NOT NULL)),
    CONSTRAINT ck_entry_known CHECK (entry_unknown OR entry_inferred OR entry_camera_id IS NOT NULL)
) PARTITION BY HASH (lot_id);
COMMENT ON TABLE parking_session IS 'Сессия стоянки. HASH по lot_id: ключ партиции входит в partial unique I-1; ретеншен 7 лет (финансовая связь), партиции по времени не дропаются — ADR-08.';
COMMENT ON COLUMN parking_session.billing_minutes IS 'Хранимое поле, не GENERATED: exit_time корректируется вручную, billing_minutes не обязано совпадать с разницей.';
COMMENT ON COLUMN parking_session.vehicle_id IS 'Меняется ручной коррекцией номера; исходное распознавание остаётся в anpr_event (entry_event_id) и manual_correction (I-16).';
COMMENT ON COLUMN parking_session.entry_event_id IS 'Событие в anpr_event: (event_time = entry_time, id = entry_event_id) — pruning по партиции месяца.';
COMMENT ON COLUMN parking_session.version IS 'Оптимистичная блокировка; UPDATE ... WHERE version = $expected.';
DO $$
BEGIN
  FOR i IN 0 .. 31 LOOP
    EXECUTE format('CREATE TABLE parking_session_p%s PARTITION OF parking_session FOR VALUES WITH (MODULUS 32, REMAINDER %s)', i, i);
  END LOOP;
END
$$;
-- I-1: проверено эмпирически (scripts/experiments/01_partition_unique.out).
CREATE UNIQUE INDEX uq_session_one_active ON parking_session (lot_id, vehicle_id) WHERE status = 'ACTIVE';
CREATE INDEX ix_session_lot_feed ON parking_session (lot_id, entry_time DESC, id DESC);
CREATE INDEX ix_session_vehicle ON parking_session (vehicle_id, entry_time DESC);
CREATE INDEX ix_session_review ON parking_session (lot_id, updated_at) WHERE status = 'MANUAL_REVIEW';
CREATE INDEX ix_session_tenant_time ON parking_session (tenant_id, entry_time);
CREATE INDEX ix_session_stale_active ON parking_session (lot_id, entry_time) WHERE status = 'ACTIVE';
COMMENT ON INDEX uq_session_one_active IS 'I-1 + горячий путь EXIT (§3.1) + occupancy (§3.6, index-only).';
COMMENT ON INDEX ix_session_lot_feed IS 'Лента оператора §3.8: курсор (entry_time, id) < ($t, $id) ORDER BY entry_time DESC, id DESC.';
CREATE TRIGGER trg_session_updated BEFORE UPDATE ON parking_session FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------- ANPR events: RANGE по месяцам ----------
CREATE TABLE anpr_event (
    id                  uuid              NOT NULL DEFAULT gen_random_uuid(),
    event_time          timestamptz       NOT NULL,
    tenant_id           uuid              NOT NULL,
    lot_id              uuid              NOT NULL,
    camera_id           uuid              NOT NULL,
    event_type          anpr_event_type   NOT NULL,
    direction           camera_direction  NOT NULL,
    received_at         timestamptz       NOT NULL DEFAULT now(),
    plate_hash          bytea             NOT NULL CHECK (octet_length(plate_hash) = 32),
    plate_encrypted     bytea             NOT NULL,
    hmac_key_version    smallint          NOT NULL DEFAULT 1,
    enc_key_version     smallint          NOT NULL DEFAULT 1,
    confidence          smallint          NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    image_object_key    text,
    recognition         jsonb             NOT NULL DEFAULT '{}'::jsonb,
    clock_skew_seconds  integer           NOT NULL DEFAULT 0,
    late                boolean           NOT NULL DEFAULT false,
    queued_offline      boolean           NOT NULL DEFAULT false,
    result              anpr_result       NOT NULL,
    reject_reason       varchar(40),
    session_id          uuid,
    saga_id             uuid,
    idempotency_key     bytea             NOT NULL CHECK (octet_length(idempotency_key) = 32),
    correlation_id      uuid              NOT NULL,
    PRIMARY KEY (event_time, id),
    CONSTRAINT fk_ae_camera FOREIGN KEY (tenant_id, lot_id, camera_id) REFERENCES camera (tenant_id, lot_id, id) ON DELETE RESTRICT
) PARTITION BY RANGE (event_time);
COMMENT ON TABLE anpr_event IS 'Принятое событие камеры — неизменяемое (только INSERT/SELECT): исходное распознавание для I-16. Ретеншен 400 дней дропом партиций (A-18).';
COMMENT ON COLUMN anpr_event.clock_skew_seconds IS 'event_time − received_at. Превышение camera.max_clock_skew_seconds не отклоняет событие, а помечает (фаза 4 §1).';
CREATE INDEX ix_ae_lot_time ON anpr_event (lot_id, event_time DESC);
CREATE INDEX ix_ae_plate ON anpr_event (tenant_id, plate_hash, event_time DESC);
CREATE INDEX ix_ae_session ON anpr_event (session_id) WHERE session_id IS NOT NULL;
SELECT app.ensure_range_partitions('anpr_event', 'month', 1, 2);

-- ---------- Идемпотентность: RANGE по дням от секунды события (§1.2, уточнение черновика) ----------
CREATE TABLE anpr_idempotency (
    idempotency_key   bytea       NOT NULL CHECK (octet_length(idempotency_key) = 32),
    event_second      timestamptz NOT NULL,
    tenant_id         uuid        NOT NULL,
    camera_id         uuid        NOT NULL,
    event_id          uuid,
    result            anpr_result NOT NULL,
    response          jsonb       NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_second, idempotency_key)
) PARTITION BY RANGE (event_second);
COMMENT ON TABLE anpr_idempotency IS 'Реестр идемпотентности (I-3). Ключ партиции event_second = floor(event_time) входит в вычисление idempotency_key, поэтому одинаковые ключи всегда попадают в одну партицию — уникальность глобальна, а ретеншен 7 суток делается дропом партиций, не DELETE.';
COMMENT ON COLUMN anpr_idempotency.response IS 'Снимок ответа первого запроса: повтор возвращает его же с duplicate=true и кодом 200.';
SELECT app.ensure_range_partitions('anpr_idempotency', 'day', 2, 3);

-- ---------- EXIT-сага ----------
CREATE TABLE exit_saga (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid        NOT NULL,
    lot_id                uuid        NOT NULL,
    session_id            uuid        NOT NULL,
    state                 saga_state  NOT NULL DEFAULT 'ACCESS_RESOLVED',
    step_deadline         timestamptz NOT NULL,
    saga_deadline         timestamptz NOT NULL,
    exit_event_id         uuid        NOT NULL,
    exit_event_time       timestamptz NOT NULL,
    additional_event_ids  uuid[]      NOT NULL DEFAULT '{}',
    access_decision       jsonb       NOT NULL,
    calculation           jsonb       NOT NULL,
    amount_due            bigint      NOT NULL CHECK (amount_due >= 0),
    payment_id            uuid,
    barrier_command_id    uuid,
    barrier_result        varchar(20),
    attempts              smallint    NOT NULL DEFAULT 0,
    last_error            text,
    review_reason         varchar(40),
    correlation_id        uuid        NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_saga_session FOREIGN KEY (tenant_id, lot_id, session_id) REFERENCES parking_session (tenant_id, lot_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_saga_tenant UNIQUE (tenant_id, id)
);
COMMENT ON TABLE exit_saga IS 'Персистентная EXIT-сага (ARCHITECTURE §4.4). Каждый шаг — своя транзакция; воркер подбирает саги с step_deadline < now().';
CREATE UNIQUE INDEX uq_saga_active_session ON exit_saga (session_id) WHERE state NOT IN ('COMPLETED', 'EXPIRED', 'CANCELLED');
COMMENT ON INDEX uq_saga_active_session IS 'A-09: второй EXIT по сессии присоединяется к живой саге, вторая сага невозможна.';
CREATE INDEX ix_saga_due ON exit_saga (step_deadline) WHERE state NOT IN ('COMPLETED', 'EXPIRED', 'CANCELLED');
CREATE INDEX ix_saga_lot_review ON exit_saga (lot_id, updated_at) WHERE state = 'MANUAL_REVIEW';
CREATE TRIGGER trg_saga_updated BEFORE UPDATE ON exit_saga FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------- Ручная коррекция номера (I-15, I-16) ----------
CREATE TABLE manual_correction (
    id                        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 uuid        NOT NULL,
    lot_id                    uuid        NOT NULL,
    session_id                uuid        NOT NULL,
    event_id                  uuid,
    old_vehicle_id            uuid        NOT NULL,
    new_vehicle_id            uuid        NOT NULL,
    corrected_plate_encrypted bytea       NOT NULL,
    enc_key_version           smallint    NOT NULL DEFAULT 1,
    reason                    text        NOT NULL CHECK (length(btrim(reason)) >= 3),
    user_id                   uuid        NOT NULL,
    correlation_id            uuid        NOT NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_mc_session FOREIGN KEY (tenant_id, lot_id, session_id) REFERENCES parking_session (tenant_id, lot_id, id),
    CONSTRAINT fk_mc_old_vehicle FOREIGN KEY (tenant_id, old_vehicle_id) REFERENCES vehicle (tenant_id, id),
    CONSTRAINT fk_mc_new_vehicle FOREIGN KEY (tenant_id, new_vehicle_id) REFERENCES vehicle (tenant_id, id),
    CONSTRAINT fk_mc_user FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id)
);
COMMENT ON TABLE manual_correction IS 'Append-only: исходное распознавание (old_vehicle_id → anpr_event) не затирается (I-16); reason и user_id обязательны (I-15).';
CREATE INDEX ix_mc_session ON manual_correction (session_id);
