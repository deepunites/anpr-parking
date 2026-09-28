"""Входящие вызовы провайдеров (фаза 5 §3): подпись → окно времени (replay) → inbox (дедуп по provider_event_id) →
поиск платежа → сумма и валюта → применение → ответ по протоколу. Payme Merchant API и Click SHOP API —
синхронные протоколы (ответ должен быть вычислен), поэтому применение — одна короткая транзакция с inbox (A-33);
тяжёлое (сага, шлагбаум) идёт через outbox асинхронно. Тот же вызов дважды → тот же сохранённый ответ, один эффект.
Вебхук раньше платежа — ORPHAN в inbox, повторное применение джобом при появлении платежа."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType
from parking.modules.billing.application.common import (
    BillingRepositories,
    ReposFactory,
    UowFactory,
    audit,
    payment_confirmed,
    payment_failed,
    record_paid,
)
from parking.modules.billing.application.ports import InboxRecord, ProviderAccount, WebhookStatus
from parking.modules.billing.application.webhook_types import (
    Action,
    InboundEvent,
    Outcome,
    WebhookProtocol,
    WebhookResult,
)
from parking.modules.billing.domain.payment import (
    AttemptStatus,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    Provider,
    Refund,
    RefundStatus,
    payment_key,
)
from parking.modules.billing.domain.reconciliation import Discrepancy, DiscrepancyKind
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.platform.secrets import SecretProvider
from parking.shared.clock import Clock
from parking.shared.errors import ValidationError

log = get_logger("billing.webhook")

REPLAY_WINDOW = timedelta(hours=1)
CREATE_WINDOW = timedelta(hours=12)  # Payme: транзакция создаётся не позже 12 ч после CheckPerformTransaction


class WebhookService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repos: ReposFactory,
        protocols: dict[Provider, WebhookProtocol],
        secrets: SecretProvider,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._protocols = protocols
        self._secrets = secrets
        self._clock = clock

    async def receive(
        self, provider: Provider, headers: dict[str, str], body: bytes, query: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        protocol = self._protocols[provider]
        try:
            event = protocol.parse(headers, body, query)
        except ValidationError as exc:
            metrics.webhook_total.labels(provider=provider.value, result="malformed").inc()
            outcome = Outcome.UNKNOWN_ACTION if exc.code == "UNKNOWN_ACTION" else Outcome.MALFORMED
            return protocol.render(_bare_event(provider), WebhookResult(outcome=outcome))
        async with self._uow_factory(None) as uow:  # pre-tenant: мерчант → tenant
            account = await self._repos(uow).accounts.by_merchant(provider, event.merchant_id)
        if account is None or not account.active:
            metrics.webhook_total.labels(provider=provider.value, result="unknown_merchant").inc()
            log.warning("webhook_unknown_merchant", provider=provider.value)
            return protocol.render(event, WebhookResult(outcome=Outcome.SIGNATURE))
        if not protocol.verify(event, headers, self._secrets.get(account.secret_ref)):
            metrics.webhook_total.labels(provider=provider.value, result="bad_signature").inc()
            await self._audit_rejected(account, event, "SIGNATURE")
            return protocol.render(event, WebhookResult(outcome=Outcome.SIGNATURE))
        if event.event_time is not None and abs(self._clock.now() - event.event_time) > (
            CREATE_WINDOW if event.action is Action.CREATE else REPLAY_WINDOW
        ):
            metrics.webhook_total.labels(provider=provider.value, result="replay").inc()
            await self._audit_rejected(account, event, "REPLAY")
            return protocol.render(event, WebhookResult(outcome=Outcome.REPLAY))
        result = await self._apply(account, event)
        metrics.webhook_total.labels(provider=provider.value, result=result.outcome.value.lower()).inc()
        return protocol.render(event, result)

    async def _audit_rejected(self, account: ProviderAccount, event: InboundEvent, why: str) -> None:
        async with self._uow_factory(account.tenant_id) as uow:
            await audit(
                self._repos(uow),
                tenant_id=account.tenant_id,
                action="WEBHOOK_REJECTED",
                entity_type="webhook",
                entity_id=account.id,
                actor=None,
                actor_type=ActorType.PROVIDER,
                reason=why,
                payload={
                    "provider": event.provider.value,
                    "event_id": event.provider_event_id,
                    "action": event.action.value,
                },
            )

    # ---------- применение ----------

    async def _apply(self, account: ProviderAccount, event: InboundEvent) -> WebhookResult:
        async with self._uow_factory(account.tenant_id) as uow:
            repos = self._repos(uow)
            record = InboxRecord(
                id=uuid4(),
                tenant_id=account.tenant_id,
                provider=event.provider,
                provider_event_id=event.provider_event_id,
                provider_transaction_id=event.provider_transaction_id,
                payload=event.payload,
                signature_valid=True,
            )
            duplicate = await repos.inbox.add(record)
            if duplicate is not None and duplicate.response is not None:  # тот же вызов дважды = тот же ответ
                if duplicate.status != WebhookStatus.ORPHAN:
                    return WebhookResult(
                        outcome=Outcome(duplicate.response["outcome"]),
                        data=_undump(duplicate.response.get("data") or {}),
                    )
                record = duplicate
            elif duplicate is not None:
                record = duplicate
            result = await self._handle(uow, repos, account, event)
            record.attempts += 1
            record.status = (
                WebhookStatus.ORPHAN
                if result.outcome is Outcome.ORDER_NOT_FOUND and event.action in {Action.PERFORM, Action.CREATE}
                else (WebhookStatus.REJECTED if result.is_error else WebhookStatus.APPLIED)
            )
            record.applied_payment_id = result.payment_id
            record.error = result.outcome.value if result.is_error else None
            record.response = {"outcome": result.outcome.value, "data": _dump(result.data)}
            await repos.inbox.save(record)
            return result

    async def replay_orphans(self, tenant_id: UUID, *, limit: int = 100) -> int:
        """Джоб: вебхуки, пришедшие раньше платежа, применяются, когда платёж появился."""
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            orphans = await repos.inbox.orphans(limit=limit)
            accounts = {p: await repos.accounts.for_tenant(p) for p in {o.provider for o in orphans}}
        applied = 0
        for orphan in orphans:
            account = accounts.get(orphan.provider)
            if account is None:
                continue
            protocol = self._protocols[orphan.provider]
            event = _event_from_record(protocol, orphan)
            if event is None:
                continue
            result = await self._apply(account, event)
            if not result.is_error:
                applied += 1
        return applied

    async def _handle(
        self, uow: UnitOfWork, repos: BillingRepositories, account: ProviderAccount, event: InboundEvent
    ) -> WebhookResult:
        if event.action is Action.CHECK:
            return await self._check(repos, event)
        if event.action is Action.CREATE:
            return await self._create(repos, event)
        if event.action is Action.PERFORM:
            return await self._perform(uow, repos, event)
        if event.action is Action.CANCEL:
            return await self._cancel(uow, repos, event)
        if event.action is Action.STATUS:
            return await self._status(repos, event)
        return await self._statement(repos, event)

    async def _order(self, repos: BillingRepositories, event: InboundEvent, *, for_update: bool) -> Payment | None:
        return await repos.payments.get(event.payment_id, for_update=for_update) if event.payment_id else None

    async def _check(self, repos: BillingRepositories, event: InboundEvent) -> WebhookResult:
        payment = await self._order(repos, event, for_update=False)
        if payment is None:
            return WebhookResult(outcome=Outcome.ORDER_NOT_FOUND)
        if payment.status is PaymentStatus.PAID:
            return WebhookResult(outcome=Outcome.ALREADY_PAID, payment_id=payment.id)
        if payment.status is not PaymentStatus.PENDING:
            return WebhookResult(outcome=Outcome.CANNOT_PERFORM, payment_id=payment.id)
        if event.amount != payment.amount:
            return WebhookResult(outcome=Outcome.AMOUNT_MISMATCH, payment_id=payment.id)
        data: dict[str, Any] = {}
        if event.provider is Provider.CLICK and event.provider_transaction_id:  # Prepare: попытка = merchant_prepare_id
            attempt = await repos.attempts.find_by_provider_tx(Provider.CLICK, event.provider_transaction_id)
            if attempt is None:
                attempt = await self._new_attempt(repos, payment, event)
            data["merchant_prepare_id"] = attempt.attempt_no
        return WebhookResult(outcome=Outcome.ALLOW, data=data, payment_id=payment.id)

    async def _create(self, repos: BillingRepositories, event: InboundEvent) -> WebhookResult:  # noqa: PLR0911 — таблица ответов протокола
        if event.provider_transaction_id is None or event.amount is None:
            return WebhookResult(outcome=Outcome.MALFORMED)  # после проверки подписи: -32600, а не 500
        existing = await repos.attempts.find_by_provider_tx(event.provider, event.provider_transaction_id)
        if existing is not None:  # идемпотентно: та же транзакция — тот же create_time
            if existing.status is AttemptStatus.FAILED:
                return WebhookResult(outcome=Outcome.CANNOT_PERFORM, payment_id=existing.payment_id)
            return WebhookResult(
                outcome=Outcome.CREATED,
                data={"create_time": existing.started_at, "transaction": str(existing.id)},
                payment_id=existing.payment_id,
            )
        payment = await self._order(repos, event, for_update=True)
        if payment is None:
            return WebhookResult(outcome=Outcome.ORDER_NOT_FOUND)
        if payment.status is PaymentStatus.PAID:
            return WebhookResult(outcome=Outcome.ALREADY_PAID, payment_id=payment.id)
        if payment.status is not PaymentStatus.PENDING:
            return WebhookResult(outcome=Outcome.CANNOT_PERFORM, payment_id=payment.id)
        if event.amount != payment.amount:
            return WebhookResult(outcome=Outcome.AMOUNT_MISMATCH, payment_id=payment.id)
        latest = await repos.attempts.latest(payment.id)
        if (
            latest is not None
            and latest.provider is event.provider
            and latest.status is AttemptStatus.PENDING
            and latest.provider_transaction_id not in (None, event.provider_transaction_id)
            and latest.started_at is not None
            and self._clock.now() - latest.started_at < CREATE_WINDOW
        ):
            return WebhookResult(outcome=Outcome.ORDER_BUSY, payment_id=payment.id)
        attempt = await self._new_attempt(repos, payment, event)
        return WebhookResult(
            outcome=Outcome.CREATED,
            data={"create_time": attempt.started_at, "transaction": str(attempt.id)},
            payment_id=payment.id,
        )

    async def _perform(self, uow: UnitOfWork, repos: BillingRepositories, event: InboundEvent) -> WebhookResult:  # noqa: PLR0911 — таблица ответов протокола
        if event.provider_transaction_id is None:
            return WebhookResult(outcome=Outcome.MALFORMED)
        attempt = await repos.attempts.find_by_provider_tx(event.provider, event.provider_transaction_id)
        payment = (
            await repos.payments.get(attempt.payment_id, for_update=True)
            if attempt
            else await self._order(repos, event, for_update=True)
        )
        if payment is None:
            return WebhookResult(outcome=Outcome.ORDER_NOT_FOUND if event.payment_id else Outcome.TX_NOT_FOUND)
        if attempt is None and event.provider is Provider.PAYME:
            return WebhookResult(
                outcome=Outcome.TX_NOT_FOUND, payment_id=payment.id
            )  # Payme: Perform только после Create
        if attempt is None:
            attempt = await self._new_attempt(repos, payment, event)
        if event.provider is Provider.CLICK and str(
            event.extra.get("merchant_prepare_id") or attempt.attempt_no
        ) != str(attempt.attempt_no):
            return WebhookResult(outcome=Outcome.TX_NOT_FOUND, payment_id=payment.id)
        if event.amount is not None and event.amount != payment.amount:
            return WebhookResult(outcome=Outcome.AMOUNT_MISMATCH, payment_id=payment.id)
        now = self._clock.now()
        if attempt.status is AttemptStatus.SUCCEEDED and payment.status in {PaymentStatus.PAID, PaymentStatus.REFUNDED}:
            return self._performed(attempt, payment)  # тот же PerformTransaction ещё раз — тот же ответ
        if attempt.status is AttemptStatus.FAILED:
            return WebhookResult(outcome=Outcome.CANNOT_PERFORM, payment_id=payment.id)
        if payment.status is PaymentStatus.PENDING:
            payment.confirm_provider(provider=event.provider, method=PaymentMethod.ONLINE, now=now)
            await repos.payments.save(payment)
            await self._succeed(repos, attempt, now)
            await audit(
                repos,
                tenant_id=payment.tenant_id,
                action="PAYMENT_PROVIDER",
                entity_type="payment",
                entity_id=payment.id,
                actor=None,
                actor_type=ActorType.PROVIDER,
                lot_id=payment.lot_id,
                session_id=payment.session_id,
                payload={
                    "provider": event.provider.value,
                    "provider_transaction_id": event.provider_transaction_id,
                    "amount": payment.amount,
                },
            )
            uow.publish(payment_confirmed(payment))
            metrics.payment_total.labels(method="ONLINE", status="PAID").inc()
            record_paid(payment)
            return self._performed(attempt, payment)
        # PAID другой транзакцией (двойная оплата) или CANCELLED/FAILED (поздний вебхук): деньги списаны — создаём
        # отдельный PAID-платёж и расхождение (очередь возврата), исходный платёж не мутируем (I-6).
        duplicate = await self._duplicate_payment(repos, payment, event, now)
        await self._succeed(repos, attempt, now)
        kind = (
            DiscrepancyKind.DUPLICATE_REMOTE
            if payment.status in {PaymentStatus.PAID, PaymentStatus.REFUNDED}
            else DiscrepancyKind.STATUS_MISMATCH
        )
        await self._open_discrepancy(
            uow,
            repos,
            duplicate,
            event,
            kind,
            {"original_payment_id": str(payment.id), "original_status": payment.status.value},
        )
        return self._performed(attempt, duplicate)

    async def _cancel(self, uow: UnitOfWork, repos: BillingRepositories, event: InboundEvent) -> WebhookResult:
        if event.provider_transaction_id is None:
            return WebhookResult(outcome=Outcome.MALFORMED)
        attempt = await repos.attempts.find_by_provider_tx(event.provider, event.provider_transaction_id)
        if attempt is None:
            return WebhookResult(outcome=Outcome.TX_NOT_FOUND)
        payment = await repos.payments.get(attempt.payment_id, for_update=True)
        assert payment is not None
        now = self._clock.now()
        reason = str(event.extra.get("reason") or event.extra.get("error") or "PROVIDER_CANCEL")
        if attempt.status is AttemptStatus.FAILED:
            return WebhookResult(
                outcome=Outcome.CANCELLED,
                data={
                    "transaction": str(attempt.id),
                    "cancel_time": attempt.finished_at,
                    "state": -1 if payment.status is not PaymentStatus.REFUNDED else -2,
                },
                payment_id=payment.id,
            )
        if payment.status is PaymentStatus.PENDING:
            attempt.status, attempt.finished_at, attempt.provider_error_code = AttemptStatus.FAILED, now, reason[:50]
            await repos.attempts.save(attempt)
            payment.fail("PROVIDER_CANCEL", provider=event.provider)
            await repos.payments.save(payment)
            uow.publish(payment_failed(payment, reason))
            return WebhookResult(
                outcome=Outcome.CANCELLED,
                data={"transaction": str(attempt.id), "cancel_time": now, "state": -1},
                payment_id=payment.id,
            )
        if (
            payment.status is PaymentStatus.PAID
        ):  # провайдер вернул деньги клиенту: компенсирующая запись, оператора нет
            refunded = sum(
                r.amount
                for r in await repos.refunds.list_for_payment(payment.id)
                if r.status is not RefundStatus.FAILED
            )
            if payment.amount - refunded > 0:
                refund = Refund(
                    id=uuid4(),
                    tenant_id=payment.tenant_id,
                    payment_id=payment.id,
                    amount=payment.amount - refunded,
                    method=PaymentMethod.ONLINE,
                    operator_id=None,
                    reason=f"provider cancel: {reason}"[:200],
                    idempotency_key=payment_key("provider-cancel", event.provider.value, event.provider_transaction_id),
                    provider_refund_id=event.provider_transaction_id,
                )
                refund.complete(now=now, provider_refund_id=event.provider_transaction_id)
                await repos.refunds.add(refund)
                await audit(
                    repos,
                    tenant_id=payment.tenant_id,
                    action="REFUND_PROVIDER",
                    entity_type="refund",
                    entity_id=refund.id,
                    actor=None,
                    actor_type=ActorType.PROVIDER,
                    lot_id=payment.lot_id,
                    session_id=payment.session_id,
                    reason=reason,
                    payload={"payment_id": str(payment.id), "amount": refund.amount},
                )
            attempt.finished_at = now
            await repos.attempts.save(attempt)
        return WebhookResult(
            outcome=Outcome.CANCELLED,
            data={"transaction": str(attempt.id), "cancel_time": now, "state": -2},
            payment_id=payment.id,
        )

    async def _status(self, repos: BillingRepositories, event: InboundEvent) -> WebhookResult:
        if event.provider_transaction_id is None:
            return WebhookResult(outcome=Outcome.MALFORMED)
        attempt = await repos.attempts.find_by_provider_tx(event.provider, event.provider_transaction_id)
        if attempt is None:
            return WebhookResult(outcome=Outcome.TX_NOT_FOUND)
        payment = await repos.payments.get(attempt.payment_id)
        assert payment is not None
        return WebhookResult(outcome=Outcome.STATUS, data=_state_of(attempt, payment), payment_id=payment.id)

    async def _statement(self, repos: BillingRepositories, event: InboundEvent) -> WebhookResult:
        start = datetime.fromtimestamp(int(event.extra.get("from") or 0) / 1000, tz=self._clock.now().tzinfo)
        end = datetime.fromtimestamp(int(event.extra.get("to") or 0) / 1000, tz=self._clock.now().tzinfo)
        rows = []
        for attempt in await repos.attempts.list_between(event.provider, start, end):
            payment = await repos.payments.get(attempt.payment_id)
            if payment is None:
                continue
            state = _state_of(attempt, payment)
            rows.append(
                {
                    "id": attempt.provider_transaction_id,
                    "time": int(attempt.started_at.timestamp() * 1000) if attempt.started_at else None,
                    "amount": payment.amount,
                    "account": {"order_id": str(payment.id)},
                    "create_time": int(attempt.started_at.timestamp() * 1000) if attempt.started_at else 0,
                    "perform_time": int(payment.paid_at.timestamp() * 1000)
                    if payment.paid_at and attempt.status is AttemptStatus.SUCCEEDED
                    else 0,
                    "cancel_time": int(attempt.finished_at.timestamp() * 1000)
                    if attempt.status is AttemptStatus.FAILED and attempt.finished_at
                    else 0,
                    "transaction": str(attempt.id),
                    "state": state["state"],
                    "reason": None,
                }
            )
        return WebhookResult(outcome=Outcome.STATEMENT, data={"transactions": rows})

    # ---------- вспомогательное ----------

    async def _new_attempt(self, repos: BillingRepositories, payment: Payment, event: InboundEvent) -> PaymentAttempt:
        attempt = PaymentAttempt(
            id=uuid4(),
            tenant_id=payment.tenant_id,
            payment_id=payment.id,
            attempt_no=await repos.attempts.next_no(payment.id),
            method=PaymentMethod.ONLINE,
            provider=event.provider,
            provider_transaction_id=event.provider_transaction_id,
            request_id=uuid4(),
            idempotency_key=payment.idempotency_key,
            request_payload={"inbound": event.action.value, "amount": event.amount},
            started_at=event.event_time or self._clock.now(),
        )
        await repos.attempts.add(attempt)
        return attempt

    async def _succeed(self, repos: BillingRepositories, attempt: PaymentAttempt, now: datetime) -> None:
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.finished_at = now
        attempt.response_payload = {**(attempt.response_payload or {}), "performed": True}
        await repos.attempts.save(attempt)

    @staticmethod
    def _performed(attempt: PaymentAttempt, payment: Payment) -> WebhookResult:
        return WebhookResult(
            outcome=Outcome.PERFORMED,
            data={
                "transaction": str(attempt.id),
                "perform_time": payment.paid_at,
                "merchant_confirm_id": attempt.attempt_no,
            },
            payment_id=payment.id,
        )

    async def _duplicate_payment(
        self, repos: BillingRepositories, original: Payment, event: InboundEvent, now: datetime
    ) -> Payment:
        key = payment_key("provider", event.provider.value, event.provider_transaction_id)
        existing = await repos.payments.find_by_key(key)
        if existing is not None:
            return existing
        duplicate = Payment(
            id=uuid4(),
            tenant_id=original.tenant_id,
            lot_id=original.lot_id,
            session_id=original.session_id,
            saga_id=None,
            amount=event.amount if event.amount is not None else original.amount,
            status=PaymentStatus.PENDING,
            idempotency_key=key,
            calculation_snapshot={**original.calculation_snapshot, "duplicate_of": str(original.id)},
        )
        duplicate.confirm_provider(provider=event.provider, method=PaymentMethod.ONLINE, now=now)
        await repos.payments.add(duplicate)
        log.error(
            "payment_duplicate_from_provider",
            original=str(original.id),
            duplicate=str(duplicate.id),
            provider=event.provider.value,
        )
        return duplicate

    async def _open_discrepancy(
        self,
        uow: UnitOfWork,
        repos: BillingRepositories,
        payment: Payment,
        event: InboundEvent,
        kind: DiscrepancyKind,
        details: dict[str, Any],
    ) -> None:
        from parking.modules.billing.application.ports import DiscrepancyRecord, ReconciliationRun  # noqa: PLC0415

        now = self._clock.now()
        run = ReconciliationRun(
            id=uuid4(),
            tenant_id=payment.tenant_id,
            provider=event.provider,
            period_start=now.date(),
            period_end=now.date(),
            started_at=now,
            finished_at=now,
            local_count=1,
            remote_count=1,
            matched_count=0,
            discrepancy_count=1,
            report={"source": "webhook", "event_id": event.provider_event_id},
        )
        await repos.reconciliation.add_run(run)
        await repos.reconciliation.save_run(run)
        record = DiscrepancyRecord(
            id=uuid4(),
            tenant_id=payment.tenant_id,
            reconciliation_id=run.id,
            discrepancy=Discrepancy(
                kind, payment.id, event.provider_transaction_id, payment.amount, event.amount, details
            ),
        )
        await repos.reconciliation.add_discrepancy(record)
        uow.publish(
            IntegrationEvent(
                type="billing.reconciliation.discrepancy_opened.v1",
                tenant_id=payment.tenant_id,
                aggregate_type="reconciliation_discrepancy",
                aggregate_id=record.id,
                payload={
                    "discrepancy_id": str(record.id),
                    "kind": kind.value,
                    "payment_id": str(payment.id),
                    "amount": payment.amount,
                },
            ),
        )
        log.error("reconciliation_discrepancy_opened", kind=kind.value, payment_id=str(payment.id))


def _state_of(attempt: PaymentAttempt, payment: Payment) -> dict[str, Any]:
    if attempt.status is AttemptStatus.FAILED:
        state = -1
    elif attempt.status is AttemptStatus.SUCCEEDED:
        state = -2 if payment.status is PaymentStatus.REFUNDED else 2
    else:
        state = 1
    return {
        "create_time": attempt.started_at,
        "perform_time": payment.paid_at if attempt.status is AttemptStatus.SUCCEEDED else None,
        "cancel_time": attempt.finished_at if state < 0 else None,
        "transaction": str(attempt.id),
        "state": state,
        "reason": None,
    }


def _dump(data: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


def _undump(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k.endswith("_time") and isinstance(v, str):
            try:
                out[k] = datetime.fromisoformat(v)
                continue
            except ValueError:
                pass
        out[k] = v
    return out


def _bare_event(provider: Provider) -> InboundEvent:
    return InboundEvent(provider=provider, merchant_id="", provider_event_id="", action=Action.STATUS)


def _event_from_record(protocol: WebhookProtocol, record: InboxRecord) -> InboundEvent | None:
    payload = record.payload
    try:
        if record.provider is Provider.PAYME:
            body = {"id": None, "method": payload["method"], "params": payload["params"]}
            import json  # noqa: PLC0415

            return protocol.parse({}, json.dumps(body).encode(), {"merchant": ""})
        from urllib.parse import urlencode  # noqa: PLC0415

        return protocol.parse({"content-type": "application/x-www-form-urlencoded"}, urlencode(payload).encode(), {})
    except ValidationError:
        return None
