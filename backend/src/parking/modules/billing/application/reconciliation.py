"""Сверка с провайдером (фаза 5 §5): наш реестр PAID/REFUNDED против реестра провайдера по provider_transaction_id.
Отчёт сохраняется целиком (доказательство, что сверка проводилась), каждое расхождение — строка со статусом и
решением. Действия по умолчанию: MISSING_LOCAL → PAID-платёж по известной сессии + алерт; MISSING_REMOTE → только
расследование (не отменять); AMOUNT_MISMATCH → очередь; STATUS_MISMATCH → подтянуть статус провайдера с аудитом;
DUPLICATE_REMOTE → очередь возврата, алерт высокого приоритета."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType
from parking.modules.billing.application.common import (
    BillingRepositories,
    ReposFactory,
    UowFactory,
    audit,
    payment_confirmed,
)
from parking.modules.billing.application.ports import (
    DiscrepancyRecord,
    GatewayRegistry,
    ReconciliationRun,
)
from parking.modules.billing.domain.payment import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    Provider,
    Refund,
    RefundStatus,
    payment_key,
)
from parking.modules.billing.domain.reconciliation import (
    Discrepancy,
    DiscrepancyKind,
    DiscrepancyStatus,
    LocalTx,
    RemoteState,
    RemoteTx,
    classify,
)
from parking.modules.identity.application.contracts import CurrentUser
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import NotFoundError, ServiceUnavailableError, ValidationError

log = get_logger("billing.reconciliation")


class ReconciliationService:
    def __init__(
        self, *, uow_factory: UowFactory, repos: ReposFactory, gateways: GatewayRegistry, clock: Clock
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._gateways = gateways
        self._clock = clock

    async def run(
        self,
        tenant_id: UUID,
        provider: Provider,
        start: date,
        end: date,
        *,
        created_by: UUID | None,
        remote: list[RemoteTx] | None = None,
    ) -> ReconciliationRun:
        """`remote` можно передать явно (реестр из файла провайдера/тест); иначе — gateway.registry() вне транзакции."""
        if end < start:
            raise ValidationError("period end is before start")
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            account = await repos.accounts.for_tenant(provider)
            local_rows = await repos.payments.settled_for_provider(provider, start, end)
        if remote is None:
            if account is None:
                raise ServiceUnavailableError(f"provider {provider.value} is not configured")
            remote = await self._gateways.gateway_for(account).registry(start, end)
        remote = [self._with_session(tx, local_rows) for tx in remote]
        local = [
            LocalTx(
                payment_id=p.id, session_id=p.session_id, provider_transaction_id=tx, amount=p.amount, status=p.status
            )
            for p, tx in local_rows
        ]
        found = classify(local, remote)
        now = self._clock.now()
        run = ReconciliationRun(
            id=uuid4(),
            tenant_id=tenant_id,
            provider=provider,
            period_start=start,
            period_end=end,
            started_at=now,
            finished_at=now,
            local_count=len(local),
            remote_count=len(remote),
            matched_count=len(
                {tx.provider_transaction_id for tx in local if tx.provider_transaction_id}
                & {r.provider_transaction_id for r in remote}
            ),
            discrepancy_count=len(found),
            created_by=created_by,
            report={
                "local": [
                    {
                        "payment_id": str(t.payment_id),
                        "tx": t.provider_transaction_id,
                        "amount": t.amount,
                        "status": t.status.value,
                    }
                    for t in local
                ],
                "remote": [
                    {
                        "tx": r.provider_transaction_id,
                        "amount": r.amount,
                        "state": r.state.value,
                        "session_id": str(r.session_id) if r.session_id else None,
                    }
                    for r in remote
                ],
                "discrepancies": [
                    {
                        "kind": d.kind.value,
                        "payment_id": str(d.payment_id) if d.payment_id else None,
                        "tx": d.provider_transaction_id,
                        "local_amount": d.local_amount,
                        "remote_amount": d.remote_amount,
                    }
                    for d in found
                ],
            },
        )
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            await repos.reconciliation.add_run(run)
            await repos.reconciliation.save_run(run)
            remote_by_tx = {r.provider_transaction_id: r for r in remote}
            for d in found:
                record = DiscrepancyRecord(id=uuid4(), tenant_id=tenant_id, reconciliation_id=run.id, discrepancy=d)
                await self._default_action(
                    uow, repos, record, remote_by_tx.get(d.provider_transaction_id or ""), provider
                )
                await repos.reconciliation.add_discrepancy(record)
                uow.publish(
                    IntegrationEvent(
                        type="billing.reconciliation.discrepancy_opened.v1",
                        tenant_id=tenant_id,
                        aggregate_type="reconciliation_discrepancy",
                        aggregate_id=record.id,
                        payload={
                            "discrepancy_id": str(record.id),
                            "kind": d.kind.value,
                            "payment_id": str(d.payment_id) if d.payment_id else None,
                            "amount": d.remote_amount or d.local_amount or 0,
                        },
                    )
                )
            uow.publish(
                IntegrationEvent(
                    type="billing.reconciliation.completed.v1",
                    tenant_id=tenant_id,
                    aggregate_type="payment_reconciliation",
                    aggregate_id=run.id,
                    payload={
                        "reconciliation_id": str(run.id),
                        "provider": provider.value,
                        "period_start": start.isoformat(),
                        "period_end": end.isoformat(),
                        "discrepancies": len(found),
                    },
                )
            )
            await audit(
                repos,
                tenant_id=tenant_id,
                action="RECONCILIATION_RUN",
                entity_type="payment_reconciliation",
                entity_id=run.id,
                actor=None,
                actor_type=ActorType.SYSTEM,
                payload={
                    "created_by": str(created_by) if created_by else None,
                    "provider": provider.value,
                    "period": [start.isoformat(), end.isoformat()],
                    "discrepancies": len(found),
                },
            )
        if found:
            log.error(
                "reconciliation_discrepancies",
                reconciliation_id=str(run.id),
                count=len(found),
                kinds=sorted({d.kind.value for d in found}),
            )
        return run

    @staticmethod
    def _with_session(tx: RemoteTx, local_rows: list[tuple[Payment, str | None]]) -> RemoteTx:
        """Провайдер знает наш order_id (= payment_id) — по нему восстанавливаем сессию для DUPLICATE_REMOTE."""
        if tx.session_id is not None:
            return tx
        by_payment = {p.id: p for p, _ in local_rows}
        by_tx = {t: p for p, t in local_rows if t}
        payment = by_payment.get(tx.payment_id) if tx.payment_id else by_tx.get(tx.provider_transaction_id)
        if payment is None:
            return tx
        return RemoteTx(tx.provider_transaction_id, tx.amount, tx.state, payment.session_id, payment.id, tx.raw)

    async def _default_action(
        self,
        uow: UnitOfWork,
        repos: BillingRepositories,
        record: DiscrepancyRecord,
        remote: RemoteTx | None,
        provider: Provider,
    ) -> None:
        d = record.discrepancy
        now = self._clock.now()
        if d.kind is DiscrepancyKind.MISSING_LOCAL and remote is not None and remote.session_id is not None:
            lot_id = await repos.payments.session_lot(remote.session_id)
            if lot_id is not None:
                payment = Payment(
                    id=uuid4(),
                    tenant_id=record.tenant_id,
                    lot_id=lot_id,
                    session_id=remote.session_id,
                    amount=remote.amount,
                    status=PaymentStatus.PENDING,
                    idempotency_key=payment_key("provider", provider.value, remote.provider_transaction_id),
                    calculation_snapshot={
                        "source": "reconciliation",
                        "provider_transaction_id": remote.provider_transaction_id,
                    },
                )
                if await repos.payments.find_by_key(payment.idempotency_key) is None:
                    payment.confirm_provider(provider=provider, method=PaymentMethod.ONLINE, now=now)
                    await repos.payments.add(payment)
                    uow.publish(payment_confirmed(payment))
                    record.discrepancy = Discrepancy(
                        d.kind,
                        payment.id,
                        d.provider_transaction_id,
                        payment.amount,
                        d.remote_amount,
                        {**d.details, "action": "payment_created"},
                    )
                    log.error("reconciliation_missing_local_created", payment_id=str(payment.id))
        elif d.kind is DiscrepancyKind.STATUS_MISMATCH and d.payment_id is not None and remote is not None:
            existing = await repos.payments.get(d.payment_id, for_update=True)
            if existing is None:
                return
            payment = existing
            if remote.state is RemoteState.PAID and payment.status is PaymentStatus.PENDING:
                payment.confirm_provider(provider=provider, method=PaymentMethod.ONLINE, now=now)
                await repos.payments.save(payment)
                uow.publish(payment_confirmed(payment))
                self._resolve(record, "status pulled from provider: PAID", now)
            elif remote.state in {RemoteState.CANCELLED, RemoteState.REFUNDED} and payment.status is PaymentStatus.PAID:
                refund = Refund(
                    id=uuid4(),
                    tenant_id=record.tenant_id,
                    payment_id=payment.id,
                    amount=payment.amount,
                    method=PaymentMethod.ONLINE,
                    operator_id=None,
                    reason=f"reconciliation: provider state {remote.state.value}",
                    idempotency_key=payment_key("recon-refund", provider.value, remote.provider_transaction_id),
                    provider_refund_id=remote.provider_transaction_id,
                )
                if await repos.refunds.find_by_key(refund.idempotency_key) is None:
                    refund.complete(now=now, provider_refund_id=remote.provider_transaction_id)
                    await repos.refunds.add(refund)
                self._resolve(
                    record, f"status pulled from provider: {remote.state.value}, compensating refund recorded", now
                )
            await audit(
                repos,
                tenant_id=record.tenant_id,
                action="RECONCILIATION_STATUS_PULLED",
                entity_type="payment",
                entity_id=payment.id,
                actor=None,
                lot_id=payment.lot_id,
                session_id=payment.session_id,
                payload={"remote_state": remote.state.value, "local_status": payment.status.value},
            )
        elif d.kind is DiscrepancyKind.DUPLICATE_REMOTE:
            log.error(
                "reconciliation_duplicate_remote", tx=d.provider_transaction_id, session=d.details.get("session_id")
            )
        elif d.kind is DiscrepancyKind.MISSING_REMOTE:
            log.warning("reconciliation_missing_remote", payment_id=str(d.payment_id))

    @staticmethod
    def _resolve(record: DiscrepancyRecord, resolution: str, now: datetime) -> None:
        """Автодействие фиксируется в details; закрыть расхождение (RESOLVED/WRITTEN_OFF) может только человек —
        ck_disc_resolved требует resolved_by."""
        d = record.discrepancy
        record.discrepancy = Discrepancy(
            d.kind,
            d.payment_id,
            d.provider_transaction_id,
            d.local_amount,
            d.remote_amount,
            {**d.details, "auto_action": resolution, "auto_action_at": now.isoformat()},
        )

    async def get_run(self, actor: CurrentUser, run_id: UUID) -> ReconciliationRun:
        async with self._uow_factory(actor.tenant_id) as uow:
            run = await self._repos(uow).reconciliation.get_run(run_id)
        if run is None:
            raise NotFoundError("reconciliation not found")
        return run

    async def discrepancies(
        self, actor: CurrentUser, *, status: DiscrepancyStatus | None, limit: int
    ) -> list[DiscrepancyRecord]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).reconciliation.list_discrepancies(status=status, limit=limit)

    async def resolve(
        self, actor: CurrentUser, record_id: UUID, *, status: DiscrepancyStatus, resolution: str
    ) -> DiscrepancyRecord:
        if status is DiscrepancyStatus.OPEN:
            raise ValidationError("resolution status must be RESOLVED or WRITTEN_OFF")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            record = await repos.reconciliation.get_discrepancy(record_id, for_update=True)
            if record is None:
                raise NotFoundError("discrepancy not found")
            record.status = status
            record.resolution = resolution
            record.resolved_by = actor.user_id
            record.resolved_at = self._clock.now()
            await repos.reconciliation.save_discrepancy(record)
            await audit(
                repos,
                tenant_id=actor.tenant_id,
                action="DISCREPANCY_RESOLVED",
                entity_type="reconciliation_discrepancy",
                entity_id=record.id,
                actor=actor,
                reason=resolution,
                payload={"status": status.value, "kind": record.discrepancy.kind.value},
            )
        return record


__all__ = ["ReconciliationService", "RefundStatus"]
