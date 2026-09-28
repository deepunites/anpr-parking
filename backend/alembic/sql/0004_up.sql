-- 0004 Tariff + Access Control.
CREATE TABLE tariff_plan (
    id          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid         NOT NULL,
    lot_id      uuid         NOT NULL,
    code        varchar(30)  NOT NULL,
    name        varchar(100) NOT NULL,
    is_default  boolean      NOT NULL DEFAULT false,
    active      boolean      NOT NULL DEFAULT true,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT fk_plan_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_plan_code UNIQUE (lot_id, code),
    CONSTRAINT uq_plan_tenant UNIQUE (tenant_id, id)
);
CREATE UNIQUE INDEX uq_plan_default ON tariff_plan (lot_id) WHERE is_default;
COMMENT ON INDEX uq_plan_default IS 'Ровно один план по умолчанию на площадку — по нему считается EXIT, если правило доступа не указывает иной.';
CREATE TRIGGER trg_plan_updated BEFORE UPDATE ON tariff_plan FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE holiday_calendar (
    id          uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid         NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    name        varchar(100) NOT NULL,
    created_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_hc_tenant UNIQUE (tenant_id, id)
);
CREATE TABLE holiday (
    tenant_id     uuid         NOT NULL,
    calendar_id   uuid         NOT NULL,
    day           date         NOT NULL,
    name          varchar(100) NOT NULL,
    PRIMARY KEY (calendar_id, day),
    CONSTRAINT fk_holiday_calendar FOREIGN KEY (tenant_id, calendar_id) REFERENCES holiday_calendar (tenant_id, id) ON DELETE CASCADE
);

-- Структурная проверка rules. Семантика (округление, окна) — в домене (фаза 3.3); БД гарантирует только форму и целочисленные деньги (I-13).
CREATE OR REPLACE FUNCTION app.tariff_rules_valid(r jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  k text;
BEGIN
  IF jsonb_typeof(r) <> 'object' THEN RETURN false; END IF;
  FOREACH k IN ARRAY ARRAY['grace_minutes', 'first_hour_price', 'price_per_hour', 'rounding_step_minutes'] LOOP
    IF jsonb_typeof(r -> k) <> 'number' OR (r ->> k) !~ '^[0-9]+$' THEN RETURN false; END IF;
  END LOOP;
  IF (r ->> 'rounding_step_minutes')::int < 1 THEN RETURN false; END IF;
  IF r ? 'daily_max' AND jsonb_typeof(r -> 'daily_max') NOT IN ('null', 'number') THEN RETURN false; END IF;
  IF r ? 'daily_max' AND jsonb_typeof(r -> 'daily_max') = 'number' AND (r ->> 'daily_max') !~ '^[0-9]+$' THEN RETURN false; END IF;
  IF NOT (r ->> 'rounding_mode') IN ('UP', 'DOWN', 'NEAREST') THEN RETURN false; END IF;
  IF r ? 'overnight' AND jsonb_typeof(r -> 'overnight') = 'object' THEN
    IF (r #>> '{overnight,start}') !~ '^[0-2][0-9]:[0-5][0-9]$' OR (r #>> '{overnight,end}') !~ '^[0-2][0-9]:[0-5][0-9]$'
       OR (r #>> '{overnight,price_per_hour}') !~ '^[0-9]+$' THEN RETURN false; END IF;
  ELSIF r ? 'overnight' AND jsonb_typeof(r -> 'overnight') <> 'null' THEN RETURN false;
  END IF;
  IF r ? 'weekday_overrides' AND jsonb_typeof(r -> 'weekday_overrides') NOT IN ('null', 'object') THEN RETURN false; END IF;
  RETURN true;
END
$$;

CREATE TABLE tariff_plan_version (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid        NOT NULL,
    plan_id             uuid        NOT NULL,
    version             integer     NOT NULL CHECK (version > 0),
    currency            char(3)     NOT NULL DEFAULT 'UZS' CHECK (currency = 'UZS'),
    rules               jsonb       NOT NULL CHECK (app.tariff_rules_valid(rules)),
    validity            tstzrange   NOT NULL CHECK (NOT isempty(validity) AND NOT lower_inf(validity)),
    holiday_calendar_id uuid,
    created_by          uuid        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_tpv_plan FOREIGN KEY (tenant_id, plan_id) REFERENCES tariff_plan (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_tpv_calendar FOREIGN KEY (tenant_id, holiday_calendar_id) REFERENCES holiday_calendar (tenant_id, id),
    CONSTRAINT fk_tpv_created_by FOREIGN KEY (tenant_id, created_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT uq_tpv UNIQUE (plan_id, version),
    CONSTRAINT uq_tpv_tenant UNIQUE (tenant_id, id),
    -- I-12. EXCLUDE не работает на партиционированных таблицах — таблица остаётся непартиционированной.
    CONSTRAINT ex_tpv_no_overlap EXCLUDE USING gist (plan_id WITH =, validity WITH &&)
);
COMMENT ON TABLE tariff_plan_version IS 'Версии тарифа неизменяемы после публикации: изменение — новая версия с новым validity. Снимок расчёта хранится в payment.calculation_snapshot (I-7).';
COMMENT ON COLUMN tariff_plan_version.rules IS 'grace_minutes, first_hour_price, price_per_hour, daily_max, rounding_mode (UP|DOWN|NEAREST), rounding_step_minutes, overnight {start,end,price_per_hour}, weekday_overrides {0..6: {...}}. Деньги — целые тийины.';
CREATE INDEX ix_tpv_plan_validity ON tariff_plan_version USING gist (plan_id, validity);

-- ADR-03: единая таблица правил доступа; вид-специфика абонемента — в satellite subscription.
CREATE TABLE subscription (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid        NOT NULL,
    lot_id        uuid,
    plate_hash    bytea       NOT NULL CHECK (octet_length(plate_hash) = 32),
    plan_code     varchar(30) NOT NULL,
    starts_at     timestamptz NOT NULL,
    ends_at       timestamptz NOT NULL CHECK (ends_at > starts_at),
    price         bigint      NOT NULL CHECK (price >= 0),
    currency      char(3)     NOT NULL DEFAULT 'UZS' CHECK (currency = 'UZS'),
    payment_id    uuid,
    auto_renew    boolean     NOT NULL DEFAULT false,
    holder_note   text,
    created_by    uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_sub_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_sub_created_by FOREIGN KEY (tenant_id, created_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT uq_sub_tenant UNIQUE (tenant_id, id)
);
COMMENT ON COLUMN subscription.payment_id IS 'Платёж за абонемент (billing). FK не ставится: payment создаётся в фазе 5, связь проверяется приложением.';
CREATE INDEX ix_sub_plate ON subscription (tenant_id, plate_hash, ends_at DESC);
CREATE TRIGGER trg_sub_updated BEFORE UPDATE ON subscription FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE access_rule (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid        NOT NULL,
    lot_id            uuid,
    kind              access_kind NOT NULL,
    plate_hash        bytea       NOT NULL CHECK (octet_length(plate_hash) = 32),
    plate_encrypted   bytea       NOT NULL,
    hmac_key_version  smallint    NOT NULL DEFAULT 1,
    enc_key_version   smallint    NOT NULL DEFAULT 1,
    validity          tstzrange   NOT NULL DEFAULT tstzrange(now(), NULL, '[)') CHECK (NOT isempty(validity)),
    discount_percent  smallint    NOT NULL DEFAULT 0,
    source_id         uuid,
    reason            text,
    created_by        uuid,
    revoked_at        timestamptz,
    revoked_by        uuid,
    revoke_reason     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_ar_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_ar_created_by FOREIGN KEY (tenant_id, created_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_ar_revoked_by FOREIGN KEY (tenant_id, revoked_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_ar_subscription FOREIGN KEY (tenant_id, source_id) REFERENCES subscription (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_ar_discount CHECK (
        (kind = 'BLACKLIST' AND discount_percent = 0)
        OR (kind = 'WHITELIST' AND discount_percent BETWEEN 1 AND 100)
        OR (kind IN ('VIP', 'SUBSCRIPTION') AND discount_percent = 100)),
    CONSTRAINT ck_ar_subscription_source CHECK (kind <> 'SUBSCRIPTION' OR source_id IS NOT NULL),
    CONSTRAINT ck_ar_revoked CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
COMMENT ON TABLE access_rule IS 'Blacklist / whitelist / VIP / абонемент как правила одного вида. lot_id IS NULL — правило всего tenant. Приоритет BLACKLIST — в AccessResolver (I-8), одним запросом по ix_access_lookup.';
COMMENT ON COLUMN access_rule.plate_encrypted IS 'Только для показа администратору. Поиск — по plate_hash.';
CREATE INDEX ix_access_lookup ON access_rule (tenant_id, plate_hash) INCLUDE (kind, lot_id, validity, discount_percent, source_id) WHERE revoked_at IS NULL;
COMMENT ON INDEX ix_access_lookup IS 'Запросы §3.2–3.3: одно обращение по (tenant_id, plate_hash), фильтр lot IS NULL OR lot = $1 и validity @> $at по INCLUDE-колонкам без обращения к heap.';
CREATE INDEX ix_access_lot_kind ON access_rule (lot_id, kind) WHERE revoked_at IS NULL;
CREATE TRIGGER trg_ar_updated BEFORE UPDATE ON access_rule FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
