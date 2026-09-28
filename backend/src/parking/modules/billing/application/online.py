"""Онлайн-оплата через провайдера (фаза 5 §3 CARD/ONLINE): попытка пишется ДО вызова, вызов — вне транзакции,
результат — после. Таймаут create() → не повторять, а спросить status(); неизвестно → попытка UNKNOWN, сага
уходит в PAYMENT_PENDING_VERIFICATION (машина не выпускается), джоб верификации спрашивает статус до ответа."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from parking.modules.billing.application.common import (
    ReposFactory,
    UowFactory,
    audit,
    check_lot_scope,
    payment_confirmed,
    payment_failed,
)
from parking.modules.billing.application.ports import (
    GatewayError,
    GatewayRegistry,
    GatewayTimeoutError,
    PaymentGateway,
    PaymentIntent,
    PaymentRequest,
    PaymentStatusResult,
    ProviderAccount,
)
from parking.modules.billing.domain.payment import (
    AttemptStatus,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    Provider,
)
from parking.modules.billing.domain.reconciliation import RemoteState
from parking.modules.identity.application.contracts import CurrentUser
from parking.platform import metrics
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.platform.tracing import FLOW_ATTR, span
from parking.shared.clock import Clock
from parking.shared.errors import ConflictError, NotFoundError, ServiceUnavailableError, ValidationError

log = get_logger("billing.online")

ACTIVE_ATTEMPT = frozenset(
    {AttemptStatus.PENDING, AttemptStatus.SUCCEEDED, AttemptStatus.UNKNOWN, AttemptStatus.TIMEOUT}
)


@dataclass(frozen=True, slots=True)
class Checkout:
    payment_id: UUID
    provider: Provider
    attempt_id: UUID
    status: AttemptStatus
    checkout_url: str | None
    provider_transaction_id: str | None


class OnlinePaymentService:
    def __init__(
        self, *, uow_factory: UowFactory, repos: ReposFactory, gateways: GatewayRegistry, clock: Clock
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._gateways = gateways
        self._clock = clock

    async def checkout(
        self, actor: CurrentUser, payment_id: UUID, provider: Provider, *, return_url: str | None
    ) -> Checkout:
        """Идемпотентно: живая попытка у этого провайдера возвращается как есть — второго create() не будет."""
        if provider is Provider.NONE:
            raise ValidationError("provider is required")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            payment = await repos.payments.get(payment_id, for_update=True)
            if payment is None or not check_lot_scope(actor, payment.lot_id):
                raise NotFoundError("payment not found")
            if payment.status is not PaymentStatus.PENDING:
                raise ConflictError(f"payment is {payment.status.value}")
            account = await repos.accounts.for_tenant(provider)
            if account is None:
                raise ServiceUnavailableError(f"provider {provider.value} is not configured for this tenant")
            latest = await repos.attempts.latest(payment.id)
            if latest is not None and latest.provider is provider and latest.status in ACTIVE_ATTEMPT:
                return self._checkout_of(
                    latest, payment, str((latest.response_payload or {}).get("checkout_url") or "")
                )
            attempt = PaymentAttempt(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                payment_id=payment.id,
                attempt_no=await repos.attempts.next_no(payment.id),
                method=PaymentMethod.ONLINE,
                provider=provider,
                request_id=uuid4(),
                idempotency_key=payment.idempotency_key,  # тот же ключ у всех попыток одного обязательства
                request_payload={"amount": payment.amount, "currency": payment.currency, "order_id": str(payment.id)},
                operator_id=actor.user_id,
                started_at=self._clock.now(),
            )
            await repos.attempts.add(attempt)  # TX-3a: попытка записана до вызова провайдера
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action="PAYMENT_CHECKOUT",
                entity_type="payment",
                entity_id=payment.id,
                actor=actor,
                lot_id=payment.lot_id,
                session_id=payment.session_id,
                payload={"provider": provider.value, "attempt_id": str(attempt.id)},
            )
        return await self._call_create(account, payment, attempt, return_url)

    async def _call_create(
        self, account: ProviderAccount, payment: Payment, attempt: PaymentAttempt, return_url: str | None
    ) -> Checkout:
        with span(
            "payment.create",
            tenant_id=payment.tenant_id,
            lot_id=payment.lot_id,
            session_id=payment.session_id,
            provider=account.provider.value,
            **{FLOW_ATTR: "exit"},
        ):
            return await self._call_create_traced(account, payment, attempt, return_url)

    async def _call_create_traced(
        self, account: ProviderAccount, payment: Payment, attempt: PaymentAttempt, return_url: str | None
    ) -> Checkout:
        gateway = self._gateways.gateway_for(account)
        request = PaymentRequest(
            payment_id=payment.id,
            session_id=payment.session_id,
            amount=payment.amount,
            currency=payment.currency,
            idempotency_key=str(payment.id),
            description=f"Parking session {payment.session_id}",
            return_url=return_url,
        )
        started = time.perf_counter()
        intent: PaymentIntent | None = None
        error: str | None = None
        status = AttemptStatus.SUCCEEDED
        try:
            intent = await gateway.create(request)
        except GatewayTimeoutError as exc:
            error = str(exc)
            status = await self._status_after_timeout(gateway, payment, attempt)
        except GatewayError as exc:
            error, status = str(exc), AttemptStatus.FAILED
        latency = int((time.perf_counter() - started) * 1000)
        return await self._record_create(payment, attempt, intent, status, error, latency)

    async def _status_after_timeout(
        self, gateway: PaymentGateway, payment: Payment, attempt: PaymentAttempt
    ) -> AttemptStatus:
        """§1 п.4: неизвестно, прошёл ли create() — спрашиваем status() по нашему ключу, но не создаём заново."""
        try:
            result: PaymentStatusResult = await gateway.status(None, idempotency_key=str(payment.id))
        except GatewayError:
            return AttemptStatus.UNKNOWN
        if result.state is None:
            return AttemptStatus.UNKNOWN
        if result.state is RemoteState.PAID:
            attempt.provider_transaction_id = result.provider_transaction_id
            return AttemptStatus.SUCCEEDED
        if result.state is RemoteState.CANCELLED:
            return AttemptStatus.FAILED
        attempt.provider_transaction_id = result.provider_transaction_id
        return AttemptStatus.PENDING

    async def _record_create(
        self,
        payment: Payment,
        attempt: PaymentAttempt,
        intent: PaymentIntent | None,
        status: AttemptStatus,
        error: str | None,
        latency: int,
    ) -> Checkout:
        async with self._uow_factory(payment.tenant_id) as uow:  # TX-3b
            repos = self._repos(uow)
            fresh = await repos.payments.get(payment.id, for_update=True)
            assert fresh is not None
            attempt.status = status
            attempt.latency_ms = latency
            attempt.finished_at = self._clock.now()
            attempt.provider_error_code = error[:50] if error else None
            if intent is not None:
                attempt.provider_transaction_id = intent.provider_transaction_id
                attempt.response_payload = {
                    "checkout_url": intent.checkout_url,
                    "state": intent.state.value,
                    **intent.raw,
                }
                attempt.status = (
                    AttemptStatus.PENDING if intent.state is RemoteState.PENDING else AttemptStatus.SUCCEEDED
                )
                if intent.state is RemoteState.PAID and fresh.status is PaymentStatus.PENDING:
                    fresh.confirm_provider(
                        provider=attempt.provider, method=PaymentMethod.ONLINE, now=self._clock.now()
                    )
                    await repos.payments.save(fresh)
                    uow.publish(payment_confirmed(fresh))
            elif status is AttemptStatus.FAILED and fresh.status is PaymentStatus.PENDING:
                fresh.fail("PROVIDER_REJECTED", provider=attempt.provider)
                await repos.payments.save(fresh)
                uow.publish(payment_failed(fresh, error or "provider rejected"))
            elif status is AttemptStatus.UNKNOWN:
                uow.publish(
                    IntegrationEvent(
                        type="billing.payment.unverified.v1",
                        tenant_id=payment.tenant_id,
                        aggregate_type="payment",
                        aggregate_id=payment.id,
                        payload={
                            "payment_id": str(payment.id),
                            "session_id": str(payment.session_id),
                            "saga_id": str(payment.saga_id) if payment.saga_id else None,
                            "provider": attempt.provider.value,
                            "attempt_id": str(attempt.id),
                        },
                    )
                )
                log.error("payment_create_unverified", payment_id=str(payment.id), provider=attempt.provider.value)
            await repos.attempts.save(attempt)
            metrics.payment_total.labels(method="ONLINE", status=attempt.status.value).inc()
        return self._checkout_of(attempt, fresh, intent.checkout_url if intent else None)

    @staticmethod
    def _checkout_of(attempt: PaymentAttempt, payment: Payment, url: str | None) -> Checkout:
        return Checkout(
            payment_id=payment.id,
            provider=attempt.provider,
            attempt_id=attempt.id,
            status=attempt.status,
            checkout_url=url or None,
            provider_transaction_id=attempt.provider_transaction_id,
        )

    async def verify_unknown(self, tenant_id: UUID, *, limit: int = 50) -> int:
        """Джоб: попытки UNKNOWN/TIMEOUT → status() провайдера; PAID → подтвердить, CANCELLED → FAILED, иначе ждать."""
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            attempts = await repos.attempts.unknown(limit=limit)
            accounts = {a.provider: await repos.accounts.for_tenant(a.provider) for a in attempts}
        resolved = 0
        for attempt in attempts:
            account = accounts.get(attempt.provider)
            if account is None:
                continue
            try:
                result = await self._gateways.gateway_for(account).status(
                    attempt.provider_transaction_id, idempotency_key=str(attempt.payment_id)
                )
            except GatewayError:
                continue
            if result.state in (None, RemoteState.PENDING):
                continue
            resolved += await self._apply_status(tenant_id, attempt.id, result)
        return resolved

    async def _apply_status(self, tenant_id: UUID, attempt_id: UUID, result: PaymentStatusResult) -> int:
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            attempt = await repos.attempts.get(attempt_id, for_update=True)
            if attempt is None or attempt.status not in {AttemptStatus.UNKNOWN, AttemptStatus.TIMEOUT}:
                return 0
            payment = await repos.payments.get(attempt.payment_id, for_update=True)
            assert payment is not None
            now = self._clock.now()
            attempt.finished_at = now
            attempt.provider_transaction_id = result.provider_transaction_id or attempt.provider_transaction_id
            attempt.response_payload = {
                **(attempt.response_payload or {}),
                "verified_state": result.state.value if result.state else None,
            }
            if result.state is RemoteState.PAID:
                attempt.status = AttemptStatus.SUCCEEDED
                if payment.status is PaymentStatus.PENDING:
                    payment.confirm_provider(provider=attempt.provider, method=PaymentMethod.ONLINE, now=now)
                    await repos.payments.save(payment)
                    uow.publish(payment_confirmed(payment))
            else:
                attempt.status = AttemptStatus.FAILED
                if payment.status is PaymentStatus.PENDING:
                    payment.fail("PROVIDER_CANCELLED", provider=attempt.provider)
                    await repos.payments.save(payment)
                    uow.publish(payment_failed(payment, "provider cancelled"))
            await repos.attempts.save(attempt)
            await audit(
                repos,
                tenant_id=tenant_id,
                action="PAYMENT_VERIFIED",
                entity_type="payment",
                entity_id=payment.id,
                actor=None,
                lot_id=payment.lot_id,
                session_id=payment.session_id,
                payload={"attempt_id": str(attempt.id), "state": result.state.value if result.state else None},
            )
            return 1
