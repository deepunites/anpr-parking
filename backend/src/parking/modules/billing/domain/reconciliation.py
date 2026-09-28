"""Сверка (фаза 5 §5): чистое сопоставление нашего реестра с реестром провайдера по provider_transaction_id."""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from parking.modules.billing.domain.payment import PaymentStatus


class DiscrepancyKind(enum.StrEnum):
    MISSING_LOCAL = "MISSING_LOCAL"  # есть у провайдера, нет у нас → создать платёж, закрыть сессию, алерт
    MISSING_REMOTE = "MISSING_REMOTE"  # у нас PAID, у провайдера нет → расследование, не отменять
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"  # очередь, ручное решение
    STATUS_MISMATCH = "STATUS_MISMATCH"  # подтянуть статус провайдера, аудит
    DUPLICATE_REMOTE = "DUPLICATE_REMOTE"  # два платежа провайдера на одну сессию → очередь возврата, алерт


class DiscrepancyStatus(enum.StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    WRITTEN_OFF = "WRITTEN_OFF"


class RemoteState(enum.StrEnum):
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class LocalTx:
    payment_id: UUID
    session_id: UUID
    provider_transaction_id: str | None
    amount: int
    status: PaymentStatus


@dataclass(frozen=True, slots=True)
class RemoteTx:
    provider_transaction_id: str
    amount: int
    state: RemoteState
    session_id: UUID | None = None  # account.order_id / merchant_trans_id, если провайдер вернул
    payment_id: UUID | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: DiscrepancyKind
    payment_id: UUID | None
    provider_transaction_id: str | None
    local_amount: int | None
    remote_amount: int | None
    details: dict[str, Any] = field(default_factory=dict)


_LOCAL_OF_REMOTE = {
    RemoteState.PAID: PaymentStatus.PAID,
    RemoteState.REFUNDED: PaymentStatus.REFUNDED,
    RemoteState.CANCELLED: PaymentStatus.CANCELLED,
    RemoteState.PENDING: PaymentStatus.PENDING,
}


def _first_paid_by_session(remote: list[RemoteTx], local_by_tx: dict[str, LocalTx]) -> dict[UUID, str]:
    paid_by_session: dict[UUID, list[RemoteTx]] = defaultdict(list)
    for r in remote:
        if r.state is RemoteState.PAID and r.session_id is not None:
            paid_by_session[r.session_id].append(r)
    first: dict[UUID, str] = {}
    for session_id, txs in paid_by_session.items():
        known = [tx for tx in txs if tx.provider_transaction_id in local_by_tx]
        first[session_id] = (known[0] if len(known) == 1 else txs[0]).provider_transaction_id
    return first


def classify(local: list[LocalTx], remote: list[RemoteTx]) -> list[Discrepancy]:
    """Сопоставление по provider_transaction_id. Один провайдерский id может встретиться у нас один раз;
    два PAID-платежа провайдера на одну сессию — DUPLICATE_REMOTE для того, которого нет у нас (иначе — для
    второго по порядку в реестре)."""
    found: list[Discrepancy] = []
    local_by_tx = {tx.provider_transaction_id: tx for tx in local if tx.provider_transaction_id}
    remote_by_tx = {tx.provider_transaction_id: tx for tx in remote}
    # «первый» платёж сессии — тот, что известен нам (по нему открыт локальный платёж); если таких нет или
    # несколько — первый по порядку в реестре провайдера. Так результат не зависит от порядка выгрузки.
    paid_sessions = _first_paid_by_session(remote, local_by_tx)
    for r in remote:
        if r.state is not RemoteState.PAID or r.session_id is None:
            continue
        first = paid_sessions[r.session_id]
        if first != r.provider_transaction_id:
            found.append(
                Discrepancy(
                    DiscrepancyKind.DUPLICATE_REMOTE,
                    local_by_tx[r.provider_transaction_id].payment_id
                    if r.provider_transaction_id in local_by_tx
                    else None,
                    r.provider_transaction_id,
                    None,
                    r.amount,
                    {"session_id": str(r.session_id), "first_transaction_id": first},
                )
            )
    for r in remote:
        mine = local_by_tx.get(r.provider_transaction_id)
        if mine is None:
            if r.state is RemoteState.PAID and not any(
                d.provider_transaction_id == r.provider_transaction_id for d in found
            ):
                found.append(
                    Discrepancy(
                        DiscrepancyKind.MISSING_LOCAL,
                        None,
                        r.provider_transaction_id,
                        None,
                        r.amount,
                        {"session_id": str(r.session_id) if r.session_id else None, "state": r.state.value},
                    )
                )
            continue
        if mine.amount != r.amount:
            found.append(
                Discrepancy(
                    DiscrepancyKind.AMOUNT_MISMATCH, mine.payment_id, r.provider_transaction_id, mine.amount, r.amount
                )
            )
        elif _LOCAL_OF_REMOTE[r.state] is not mine.status and not (
            mine.status is PaymentStatus.REFUNDED and r.state is RemoteState.PAID
        ):
            found.append(
                Discrepancy(
                    DiscrepancyKind.STATUS_MISMATCH,
                    mine.payment_id,
                    r.provider_transaction_id,
                    mine.amount,
                    r.amount,
                    {"local_status": mine.status.value, "remote_state": r.state.value},
                )
            )
    for tx in local:
        if tx.status in {PaymentStatus.PAID, PaymentStatus.REFUNDED} and (
            tx.provider_transaction_id is None or tx.provider_transaction_id not in remote_by_tx
        ):
            found.append(
                Discrepancy(DiscrepancyKind.MISSING_REMOTE, tx.payment_id, tx.provider_transaction_id, tx.amount, None)
            )
    return found
