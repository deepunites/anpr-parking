# Платежи, возвраты, сверка (фаза 5)

Код: `modules/billing` (domain: `payment.py`, `reconciliation.py`; application: `cash.py`, `online.py`, `refunds.py`,
`webhooks.py`, `reconciliation.py`; infrastructure: `repositories.py`, `gateways.py`, `webhooks.py`, `scanner.py`),
`api/v1/payments.py`, задачи `parking.payments.*` в `workers/celery_app.py`, миграция 0012.
Gate — `docs/backend/gate-5.out`. Допущения — A-32…A-36.

## 1. Принципы в коде

| Принцип (§1) | Где принуждается |
|---|---|
| Терминальный платёж не мутируется (I-6) | триггер `app.payment_guard` (`UPDATE`/`DELETE` PAID/REFUNDED/FAILED/CANCELLED → `check_violation`), `Payment._require_pending()`; коррекция — только `Refund`; `REFUNDED` — производный (`app.refund_settle`) |
| Провайдер — истина по факту оплаты, мы — по обязательству | вебхук/`status()` подтверждают PENDING; расхождение → `ReconciliationDiscrepancy`, не правка |
| Идемпотентность на каждой границе | `payment.idempotency_key` (UNIQUE), `refund.idempotency_key`, `webhook_inbox (provider, provider_event_id)` UNIQUE + сохранённый ответ, попытка провайдера с одним ключом на обязательство |
| Ретрай неидемпотентной операции запрещён | `OnlinePaymentService`: таймаут `create()` → `status()` по нашему ключу; неизвестно → попытка `UNKNOWN`, второй `create()` невозможен (живая попытка возвращается как есть) |
| Сумма считается на бэкенде | `CashierService._settle_amount`: `amount_minor` клиента — только сверка, расхождение → `422 AMOUNT_MISMATCH`; нестандартная сумма — `payment:create_cash_custom` + reason, только для предоплаты (обязательство саги не переписывается) |

## 2. Наличные и смены (`cash.py`)

- `POST /api/v1/shifts` / `…/{id}/close` / `GET /shifts/current` — одна OPEN-смена на кассира (`uq_shift_open_per_cashier`),
  `expected_cash = opening_float + CASH-платежи смены − CASH-возвраты смены`, `discrepancy = counted − expected` хранится
  как есть (инкассация сверяет, не подгоняет), событие `billing.shift.closed.v1`, аудит с суммами.
- `POST /api/v1/payments/{id}/confirm-cash` — PENDING-обязательство саги → PAID (operator, shift той же площадки,
  `ck_cash_has_operator_shift`), `payment_attempt(CASH)`, аудит `PAYMENT_CASH`, `billing.payment.confirmed.v1` → сага →
  шлагбаум. Повтор → тот же PAID-платёж (двойная отправка = одно списание).
- `POST /api/v1/sessions/{id}/payments` — предоплата (A-11, SD-2b): `Parking.prepay_quote` (тариф + решение доступа),
  PAID сразу с `paid_until = now + lot.exit_grace_minutes`; EXIT в окне — `EXIT_CONFIRMED` без доплаты; повтор в окне —
  тот же платёж. Оплатили, но уехали без камеры — джоб `parking.maintenance.sweep_prepaid`: через
  `exit_inferred_after_minutes` после окна сессия закрывается как `INFERRED_BY_PAYMENT`, `exit_time = paid_at`, аудит
  `SESSION_EXIT_INFERRED`.

## 3. CARD / ONLINE (`online.py`, `gateways.py`)

Порт `PaymentGateway.create/status/refund/registry`; адаптеры `PaymeGateway` (Subscribe API `receipts.*`, `X-Auth`)
и `ClickGateway` (Merchant API `invoice/create`, `payment/status_by_mti`, `payment/reversal`, `Auth: user:sha1(ts+secret):ts`)
написаны по публичной документации и **требуют подтверждения на тестовых аккаунтах** (Q1); секреты — по
`provider_account.secret_ref` из `SecretProvider` (Vault в проде), никогда в логах и `payment_attempt.request_payload`.
Общее: таймаут 5 с, breaker на провайдера (5 → OPEN 60 с), метрики `gateway_calls_total{provider,result}`, `gateway_seconds`.

`POST /api/v1/payments/{id}/checkout {provider}`: TX-3a — попытка `PENDING` с запросом без секретов **до** вызова;
`create()` вне транзакции; TX-3b — результат (`provider_transaction_id`, ответ, латентность). Живая попытка у того же
провайдера возвращается без второго `create()`. Таймаут → `status(idempotency_key=payment_id)`: PAID → подтверждено;
неизвестно → попытка `UNKNOWN`, событие `billing.payment.unverified.v1`, сага `PAYMENT_PENDING_VERIFICATION`
(машина не выпускается, `parking.session.manual_review.v1` оператору); джоб `parking.payments.verify_pending`
раз в минуту спрашивает `status()` до ответа; дедлайн шага → `MANUAL_REVIEW`.

## 4. Вебхуки и колбэки (`webhooks.py`)

`POST /api/v1/payments/webhook/{provider}`: Payme Merchant API (JSON-RPC, Basic `Paycom:<key>`, мерчант —
`?merchant=<id>` из кабинета) и Click SHOP API (Prepare/Complete, `sign_string = md5(…secret…)`). Порядок:
подпись → окно времени (Payme `time` ±12 ч на CreateTransaction, Click `sign_time` ±1 ч) → `webhook_inbox`
(дедуп по `(provider, provider_event_id)`, ответ сохраняется: тот же вызов дважды = тот же ответ, один эффект) →
платёж по `provider_transaction_id` (попытка) или `order_id`/`merchant_trans_id` → сумма и валюта → применение →
ответ по протоколу. Оба протокола синхронные (ответ вычисляется), поэтому применение — одна короткая транзакция с
inbox (A-33); сага и шлагбаум — асинхронно через outbox. Ошибка подписи → `401`-эквивалент протокола (`-32504` / `-1`)
и аудит `WEBHOOK_REJECTED` (actor PROVIDER). Вебхук раньше платежа → `ORPHAN` в inbox, `parking.payments.replay_orphans`
применяет его при появлении платежа. Двойная оплата (второй `provider_transaction_id` на уже PAID-заказ, деньги списаны)
→ отдельный PAID-платёж + `ReconciliationDiscrepancy(DUPLICATE_REMOTE)` = очередь возврата + событие/алерт; поздний
вебхук на CANCELLED → PAID-платёж + `STATUS_MISMATCH` (решение оператора: возврат или зачёт). Payme `CancelTransaction`
после оплаты → компенсирующий `refund DONE` без оператора (`ck_refund_operator`), REFUNDED производно.

## 5. Возвраты (`refunds.py`)

`POST /api/v1/payments/{id}/refunds {amount_minor, reason}` (+ `Idempotency-Key`), `payment:refund`. Только PAID,
сумма ≤ платёж − уже возвращённое: проверка в коде (`check_refund`) и в БД (`app.refund_guard` под `FOR UPDATE`
платежа), частичные допустимы, статус REFUNDED выставляется триггером, когда сумма DONE-возвратов равна платежу.
CASH — из открытой смены возвращающего, DONE сразу; CARD/ONLINE — PENDING → `parking.payments.process_refunds`
→ `gateway.refund` вне транзакции (таймаут → остаётся PENDING, повторный вызов только после статуса) → DONE/FAILED,
события `billing.refund.created/completed/failed.v1`. Property-based тест (hypothesis, 300 примеров) + случайные
последовательности против триггера: сумма возвратов никогда не превышает платёж.

## 6. Сверка (`reconciliation.py`)

`parking.payments.reconcile_daily` (вчера, каждый активный мерчант-аккаунт) и `POST /api/v1/reconciliations`
(`reconciliation:manage`). Наш реестр PAID/REFUNDED с `provider_transaction_id` из попыток ↔ `gateway.registry(period)`
(Payme `receipts.get_all`; у Click пакетной выгрузки нет — A-35). Чистая функция `classify()` даёт 5 классов;
действия по умолчанию: `MISSING_LOCAL` → PAID-платёж по известной сессии + алерт; `MISSING_REMOTE` → расследование,
**не** отменяем; `AMOUNT_MISMATCH` → очередь; `STATUS_MISMATCH` → подтянуть статус провайдера (PAID → подтвердить,
CANCELLED/REFUNDED → компенсирующий возврат) + аудит `RECONCILIATION_STATUS_PULLED`; `DUPLICATE_REMOTE` → очередь
возврата + алерт. Каждое расхождение — строка `OPEN/RESOLVED/WRITTEN_OFF` с исполнителем и решением
(`POST /api/v1/reconciliation-discrepancies/{id}/resolve`); автодействие пишется в `details.auto_action`, закрыть может
только человек (`ck_disc_resolved`). Отчёт `payment_reconciliation.report` хранит **весь** список local/remote/discrepancies.
Метрика `reconciliation_discrepancies_open{tenant}`, событие `billing.reconciliation.completed.v1`.

## 7. Интеграция с EXIT-сагой (§6) — покрыто тестами

| Случай | Поведение | Тест |
|---|---|---|
| Провайдер не ответил | не ретраить `create()`; `status()`; неизвестно → `PAYMENT_PENDING_VERIFICATION`, машина стоит, оператор уведомлён; `verify_pending` добивает | `test_online.py::test_provider_timeout_*` |
| Платёж прошёл, шлагбаум не открылся | платёж PAID остаётся, сага `MANUAL_REVIEW`, возврата нет | `test_online.py::test_barrier_failure_after_payment_keeps_payment` |
| Уехал через соседний выезд | `sweep_prepaid` → `INFERRED_BY_PAYMENT`, `exit_time = paid_at` | `test_cash.py::test_paid_but_left_*` |
| Оплата прошла дважды | второй платёж PAID + `DUPLICATE_REMOTE` (очередь возврата), алерт | `test_webhooks.py::test_click_prepare_complete_and_duplicate_*` |

## 8. Матрица отказов фазы

| Отказ | Поведение |
|---|---|
| Провайдер недоступен (breaker OPEN) | checkout → 502-эквивалент `GatewayError`, попытка FAILED не ставится (breaker до вызова) — кассир принимает наличные |
| Вебхук с чужим мерчантом/подписью | отказ по протоколу, аудит, никакого inbox |
| Redis недоступен | платежи не зависят от Redis; события через outbox |
| PostgreSQL недоступен | вебхук получит 5xx — провайдеры ретраят (Payme/Click повторяют колбэки) |
