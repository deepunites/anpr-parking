-- 0006 Billing.
CREATE TABLE provider_account (
    id            uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid             NOT NULL REFERENCES tenant (id) ON DELETE RESTRICT,
    provider      payment_provider NOT NULL CHECK (provider <> 'NONE'),
    merchant_id   varchar(100)     NOT NULL,
    secret_ref    varchar(255)     NOT NULL,
    active        boolean          NOT NULL DEFAULT true,
    created_at    timestamptz      NOT NULL DEFAULT now(),
    updated_at    timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT uq_pa_merchant UNIQUE (provider, merchant_id),
    CONSTRAINT uq_pa_tenant_provider UNIQUE (tenant_id, provider)
);
COMMENT ON TABLE provider_account IS 'Мерчант-аккаунт tenant у провайдера. Секрет подписи вебхуков — в Vault (secret_ref).';
CREATE TRIGGER trg_pa_updated BEFORE UPDATE ON provider_account FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Pre-tenant lookup: вебхук провайдера приходит до того, как известен tenant.
CREATE OR REPLACE FUNCTION app.lookup_provider_account(p_provider payment_provider, p_merchant_id text)
RETURNS TABLE (id uuid, tenant_id uuid, secret_ref varchar, active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT a.id, a.tenant_id, a.secret_ref, a.active FROM provider_account a
  WHERE a.provider = p_provider AND a.merchant_id = p_merchant_id
$$;
REVOKE ALL ON FUNCTION app.lookup_provider_account(payment_provider, text) FROM PUBLIC;

CREATE TABLE cashier_shift (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid         NOT NULL,
    lot_id          uuid         NOT NULL,
    cashier_id      uuid         NOT NULL,
    status          shift_status NOT NULL DEFAULT 'OPEN',
    opened_at       timestamptz  NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    opening_float   bigint       NOT NULL DEFAULT 0 CHECK (opening_float >= 0),
    expected_cash   bigint,
    counted_cash    bigint,
    discrepancy     bigint       GENERATED ALWAYS AS (counted_cash - expected_cash) STORED,
    collected_by    uuid,
    notes           text,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT fk_shift_lot FOREIGN KEY (tenant_id, lot_id) REFERENCES parking_lot (tenant_id, id),
    CONSTRAINT fk_shift_cashier FOREIGN KEY (tenant_id, cashier_id) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_shift_collected_by FOREIGN KEY (tenant_id, collected_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT uq_shift_tenant UNIQUE (tenant_id, id),
    CONSTRAINT ck_shift_closed CHECK (status <> 'CLOSED' OR (closed_at IS NOT NULL AND expected_cash IS NOT NULL AND counted_cash IS NOT NULL))
);
COMMENT ON TABLE cashier_shift IS 'Смена кассира: наличные без смены не сверяются (фаза 5 §3). expected_cash считается на закрытии из платежей и возвратов смены.';
CREATE UNIQUE INDEX uq_shift_open_per_cashier ON cashier_shift (cashier_id) WHERE status = 'OPEN';
CREATE INDEX ix_shift_lot_time ON cashier_shift (lot_id, opened_at DESC);
CREATE TRIGGER trg_shift_updated BEFORE UPDATE ON cashier_shift FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE payment (
    id                    uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid             NOT NULL,
    lot_id                uuid             NOT NULL,
    session_id            uuid             NOT NULL,
    saga_id               uuid,
    amount                bigint           NOT NULL CHECK (amount >= 0),
    currency              char(3)          NOT NULL DEFAULT 'UZS' CHECK (currency = 'UZS'),
    status                payment_status   NOT NULL DEFAULT 'PENDING',
    method                payment_method   NOT NULL,
    provider              payment_provider NOT NULL DEFAULT 'NONE',
    operator_id           uuid,
    shift_id              uuid,
    source_id             uuid,
    calculation_snapshot  jsonb            NOT NULL,
    idempotency_key       bytea            NOT NULL CHECK (octet_length(idempotency_key) = 32),
    paid_at               timestamptz,
    paid_until            timestamptz,
    cancel_reason         varchar(60),
    created_at            timestamptz      NOT NULL DEFAULT now(),
    updated_at            timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT uq_payment_idem UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT uq_payment_tenant UNIQUE (tenant_id, id),
    CONSTRAINT fk_payment_session FOREIGN KEY (tenant_id, lot_id, session_id) REFERENCES parking_session (tenant_id, lot_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_payment_operator FOREIGN KEY (tenant_id, operator_id) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_payment_shift FOREIGN KEY (tenant_id, shift_id) REFERENCES cashier_shift (tenant_id, id),
    CONSTRAINT ck_cash_has_operator_shift CHECK (method <> 'CASH' OR (operator_id IS NOT NULL AND shift_id IS NOT NULL)),
    CONSTRAINT ck_paid_has_time CHECK (status <> 'PAID' OR paid_at IS NOT NULL),
    CONSTRAINT ck_privilege_is_zero CHECK (method NOT IN ('SUBSCRIPTION', 'VIP', 'WHITELIST', 'GRACE') OR amount = 0),
    CONSTRAINT ck_privilege_source CHECK (method NOT IN ('SUBSCRIPTION', 'VIP', 'WHITELIST') OR source_id IS NOT NULL),
    CONSTRAINT ck_provider_methods CHECK ((method IN ('CARD', 'ONLINE')) = (provider <> 'NONE'))
);
COMMENT ON TABLE payment IS 'Обязательство: сколько и за что. PAID/REFUNDED никогда не мутируется (I-6, триггер app.payment_guard); коррекции — только refund.';
COMMENT ON COLUMN payment.calculation_snapshot IS 'Полный breakdown расчёта тарифа на момент создания (I-7): изменение тарифа задним числом не меняет исторические платежи.';
COMMENT ON COLUMN payment.paid_until IS 'A-11: предоплата — до какого момента можно выехать без доплаты (paid_at + lot.exit_grace_minutes).';
COMMENT ON COLUMN payment.source_id IS 'Основание льготы (access_rule.id / subscription.id): бесплатный проезд виден в отчётах как использование льготы, а не как отсутствие платежа.';
CREATE INDEX ix_payment_session ON payment (session_id);
CREATE INDEX ix_payment_report ON payment (tenant_id, paid_at) INCLUDE (method, amount, lot_id) WHERE status = 'PAID';
CREATE INDEX ix_payment_pending ON payment (created_at) WHERE status = 'PENDING';
CREATE INDEX ix_payment_shift ON payment (shift_id) WHERE shift_id IS NOT NULL;
CREATE INDEX ix_payment_saga ON payment (saga_id) WHERE saga_id IS NOT NULL;
COMMENT ON INDEX ix_payment_report IS 'Дневной отчёт §3.7: диапазон paid_at по tenant, группировка по method — index-only по INCLUDE.';

-- I-6: терминальные статусы неизменяемы. Разрешено: PENDING → PAID|FAILED|CANCELLED (и правка PENDING), PAID → REFUNDED
-- только сменой статуса (все прочие колонки равны) — выставляется производно триггером refund.
CREATE OR REPLACE FUNCTION app.payment_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_old jsonb := to_jsonb(OLD) - 'status' - 'updated_at';
  v_new jsonb := to_jsonb(NEW) - 'status' - 'updated_at';
BEGIN
  IF OLD.status = 'PENDING' THEN
    IF NEW.status NOT IN ('PENDING', 'PAID', 'FAILED', 'CANCELLED') THEN
      RAISE EXCEPTION '[I-6] payment %: переход PENDING → % запрещён', OLD.id, NEW.status USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.amount <> OLD.amount OR NEW.session_id <> OLD.session_id OR NEW.idempotency_key <> OLD.idempotency_key THEN
      RAISE EXCEPTION '[I-6] payment %: сумма, сессия и ключ идемпотентности неизменяемы', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status = 'PAID' AND NEW.status = 'REFUNDED' AND v_old = v_new THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION '[I-6] payment % в терминальном статусе % не изменяется (попытка: status=%)', OLD.id, OLD.status, NEW.status
    USING ERRCODE = 'check_violation';
END
$$;
CREATE TRIGGER trg_payment_guard BEFORE UPDATE ON payment FOR EACH ROW EXECUTE FUNCTION app.payment_guard();
CREATE TRIGGER trg_payment_updated BEFORE UPDATE ON payment FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
CREATE OR REPLACE FUNCTION app.forbid_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '[I-6] DELETE из % запрещён', TG_TABLE_NAME USING ERRCODE = 'check_violation';
END
$$;
CREATE TRIGGER trg_payment_no_delete BEFORE DELETE ON payment FOR EACH ROW EXECUTE FUNCTION app.forbid_delete();

-- Попытки: ~1 млн/сутки, финансовый ретеншен 7 лет → партиции по месяцам.
CREATE TABLE payment_attempt (
    id                        uuid             NOT NULL DEFAULT gen_random_uuid(),
    created_at                timestamptz      NOT NULL DEFAULT now(),
    tenant_id                 uuid             NOT NULL,
    payment_id                uuid             NOT NULL,
    attempt_no                smallint         NOT NULL CHECK (attempt_no > 0),
    method                    payment_method   NOT NULL,
    provider                  payment_provider NOT NULL DEFAULT 'NONE',
    provider_transaction_id   varchar(100),
    request_id                uuid             NOT NULL,
    idempotency_key           bytea,
    request_payload           jsonb,
    response_payload          jsonb,
    http_status               smallint,
    provider_error_code       varchar(50),
    status                    attempt_status   NOT NULL DEFAULT 'PENDING',
    latency_ms                integer,
    started_at                timestamptz      NOT NULL DEFAULT now(),
    finished_at               timestamptz,
    shift_id                  uuid,
    operator_id               uuid,
    PRIMARY KEY (created_at, id),
    CONSTRAINT fk_attempt_payment FOREIGN KEY (tenant_id, payment_id) REFERENCES payment (tenant_id, id) ON DELETE RESTRICT
) PARTITION BY RANGE (created_at);
COMMENT ON TABLE payment_attempt IS 'Каждая попытка провести платёж: запрос (без секретов), ответ, код провайдера, латентность. Это то, что спасёт при разборе спорной транзакции через полгода.';
CREATE INDEX ix_attempt_payment ON payment_attempt (payment_id, attempt_no);
CREATE INDEX ix_attempt_provider_tx ON payment_attempt (provider, provider_transaction_id) WHERE provider_transaction_id IS NOT NULL;
SELECT app.ensure_range_partitions('payment_attempt', 'month', 1, 2);

CREATE TABLE refund (
    id                  uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid           NOT NULL,
    payment_id          uuid           NOT NULL,
    amount              bigint         NOT NULL CHECK (amount > 0),
    currency            char(3)        NOT NULL DEFAULT 'UZS' CHECK (currency = 'UZS'),
    status              refund_status  NOT NULL DEFAULT 'PENDING',
    method              payment_method NOT NULL CHECK (method IN ('CASH', 'CARD', 'ONLINE')),
    shift_id            uuid,
    operator_id         uuid           NOT NULL,
    reason              text           NOT NULL CHECK (length(btrim(reason)) >= 3),
    provider_refund_id  varchar(100),
    idempotency_key     bytea          NOT NULL CHECK (octet_length(idempotency_key) = 32),
    created_at          timestamptz    NOT NULL DEFAULT now(),
    updated_at          timestamptz    NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    CONSTRAINT fk_refund_payment FOREIGN KEY (tenant_id, payment_id) REFERENCES payment (tenant_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_refund_shift FOREIGN KEY (tenant_id, shift_id) REFERENCES cashier_shift (tenant_id, id),
    CONSTRAINT fk_refund_operator FOREIGN KEY (tenant_id, operator_id) REFERENCES app_user (tenant_id, id),
    CONSTRAINT uq_refund_idem UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT ck_refund_cash_shift CHECK (method <> 'CASH' OR shift_id IS NOT NULL)
);
COMMENT ON TABLE refund IS 'Возврат — компенсирующая запись со ссылкой на платёж. Сумма всех незавершившихся отказом возвратов ≤ payment.amount — проверяется триггером под блокировкой платежа.';
CREATE INDEX ix_refund_payment ON refund (payment_id);
CREATE TRIGGER trg_refund_updated BEFORE UPDATE ON refund FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE OR REPLACE FUNCTION app.refund_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_payment_amount bigint;
  v_payment_status payment_status;
  v_refunded       bigint;
BEGIN
  -- блокируем платёж: параллельные возвраты сериализуются
  SELECT amount, status INTO v_payment_amount, v_payment_status FROM payment WHERE id = NEW.payment_id FOR UPDATE;
  IF v_payment_status IS NULL THEN
    RAISE EXCEPTION 'refund: платёж % не найден', NEW.payment_id USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF v_payment_status NOT IN ('PAID', 'REFUNDED') THEN
    RAISE EXCEPTION 'refund: платёж % в статусе %, возврат возможен только из PAID', NEW.payment_id, v_payment_status USING ERRCODE = 'check_violation';
  END IF;
  SELECT coalesce(sum(amount), 0) INTO v_refunded FROM refund
   WHERE payment_id = NEW.payment_id AND status <> 'FAILED' AND (TG_OP = 'INSERT' OR id <> NEW.id);
  IF NEW.status <> 'FAILED' AND v_refunded + NEW.amount > v_payment_amount THEN
    RAISE EXCEPTION 'refund: сумма возвратов % + % превышает платёж %', v_refunded, NEW.amount, v_payment_amount USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_refund_guard BEFORE INSERT OR UPDATE ON refund FOR EACH ROW EXECUTE FUNCTION app.refund_guard();

-- Производный статус REFUNDED: когда сумма завершённых возвратов равна платежу.
CREATE OR REPLACE FUNCTION app.refund_settle() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_done bigint;
  v_amount bigint;
BEGIN
  SELECT coalesce(sum(amount), 0) INTO v_done FROM refund WHERE payment_id = NEW.payment_id AND status = 'DONE';
  SELECT amount INTO v_amount FROM payment WHERE id = NEW.payment_id;
  IF v_done = v_amount AND v_amount > 0 THEN
    UPDATE payment SET status = 'REFUNDED' WHERE id = NEW.payment_id AND status = 'PAID';
  END IF;
  RETURN NULL;
END
$$;
CREATE TRIGGER trg_refund_settle AFTER INSERT OR UPDATE OF status ON refund FOR EACH ROW EXECUTE FUNCTION app.refund_settle();

CREATE TABLE payment_reconciliation (
    id                 uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid             NOT NULL REFERENCES tenant (id),
    provider           payment_provider NOT NULL,
    period_start       date             NOT NULL,
    period_end         date             NOT NULL CHECK (period_end >= period_start),
    started_at         timestamptz      NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    local_count        integer,
    remote_count       integer,
    matched_count      integer,
    discrepancy_count  integer,
    report             jsonb            NOT NULL DEFAULT '{}'::jsonb,
    created_by         uuid,
    CONSTRAINT uq_recon_tenant UNIQUE (tenant_id, id)
);
COMMENT ON COLUMN payment_reconciliation.report IS 'Отчёт сверки целиком, не только расхождения — иначе невозможно доказать, что сверка проводилась.';
CREATE INDEX ix_recon_tenant_period ON payment_reconciliation (tenant_id, period_start DESC);

CREATE TABLE reconciliation_discrepancy (
    id                        uuid               PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 uuid               NOT NULL,
    reconciliation_id         uuid               NOT NULL,
    kind                      discrepancy_kind   NOT NULL,
    status                    discrepancy_status NOT NULL DEFAULT 'OPEN',
    payment_id                uuid,
    provider_transaction_id   varchar(100),
    local_amount              bigint,
    remote_amount             bigint,
    details                   jsonb              NOT NULL DEFAULT '{}'::jsonb,
    assigned_to               uuid,
    resolution                text,
    resolved_by               uuid,
    resolved_at               timestamptz,
    created_at                timestamptz        NOT NULL DEFAULT now(),
    updated_at                timestamptz        NOT NULL DEFAULT now(),
    CONSTRAINT fk_disc_recon FOREIGN KEY (tenant_id, reconciliation_id) REFERENCES payment_reconciliation (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_disc_payment FOREIGN KEY (tenant_id, payment_id) REFERENCES payment (tenant_id, id),
    CONSTRAINT fk_disc_assigned FOREIGN KEY (tenant_id, assigned_to) REFERENCES app_user (tenant_id, id),
    CONSTRAINT fk_disc_resolved_by FOREIGN KEY (tenant_id, resolved_by) REFERENCES app_user (tenant_id, id),
    CONSTRAINT ck_disc_resolved CHECK (status = 'OPEN' OR (resolved_by IS NOT NULL AND resolved_at IS NOT NULL AND resolution IS NOT NULL))
);
CREATE INDEX ix_disc_open ON reconciliation_discrepancy (tenant_id, kind) WHERE status = 'OPEN';
CREATE TRIGGER trg_disc_updated BEFORE UPDATE ON reconciliation_discrepancy FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE webhook_inbox (
    id                        uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 uuid             NOT NULL REFERENCES tenant (id),
    provider                  payment_provider NOT NULL,
    provider_event_id         varchar(120)     NOT NULL,
    provider_transaction_id   varchar(100),
    payload                   jsonb            NOT NULL,
    signature_valid           boolean          NOT NULL,
    status                    webhook_status   NOT NULL DEFAULT 'NEW',
    applied_payment_id        uuid,
    error                     text,
    received_at               timestamptz      NOT NULL DEFAULT now(),
    processed_at              timestamptz,
    attempts                  smallint         NOT NULL DEFAULT 0,
    CONSTRAINT uq_webhook_event UNIQUE (provider, provider_event_id)
);
COMMENT ON TABLE webhook_inbox IS 'Вебхук пишется целиком до обработки (быстрый 200). Дедуп по provider_event_id; ORPHAN — платёж с таким provider_transaction_id ещё не сохранён, повторная попытка позже.';
CREATE INDEX ix_webhook_pending ON webhook_inbox (received_at) WHERE status IN ('NEW', 'ORPHAN');
CREATE INDEX ix_webhook_tx ON webhook_inbox (provider, provider_transaction_id) WHERE provider_transaction_id IS NOT NULL;
