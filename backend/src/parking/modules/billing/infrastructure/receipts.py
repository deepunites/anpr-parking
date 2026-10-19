"""Номера чеков: реестр receipt, счётчик площадки parking_lot.receipt_seq. Выдача — под блокировкой строки площадки:
два кассира, печатающие одновременно, получают соседние номера, повторная печать того же платежа — тот же номер,
пропусков нет (откат транзакции откатывает и счётчик)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import text

from parking.platform.db.uow import UnitOfWork

_LOCK_LOT = text("SELECT 1 FROM parking_lot WHERE id = :lot FOR UPDATE")
_NEXT = text("UPDATE parking_lot SET receipt_seq = receipt_seq + 1 WHERE id = :lot RETURNING receipt_seq")
# источник чека — оплата стоянки или продажа абонемента; запросы — константы на каждый случай (без f-строк в SQL)
_FIND = {
    "payment": text("SELECT receipt_no FROM receipt WHERE payment_id = :source"),
    "subscription": text("SELECT receipt_no FROM receipt WHERE subscription_id = :source"),
}
_ADD = {
    "payment": text(
        "INSERT INTO receipt (tenant_id, lot_id, receipt_no, payment_id, amount, issued_by) "
        "VALUES (:tenant, :lot, :receipt_no, :source, :amount, :by)"
    ),
    "subscription": text(
        "INSERT INTO receipt (tenant_id, lot_id, receipt_no, subscription_id, amount, issued_by) "
        "VALUES (:tenant, :lot, :receipt_no, :source, :amount, :by)"
    ),
}


class SqlReceiptNumbers:
    def __init__(self, uow_factory: Callable[[UUID | None], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def for_payment(
        self, tenant_id: UUID, lot_id: UUID, payment_id: UUID, *, amount: int, issued_by: UUID | None
    ) -> int:
        return await self._issue(tenant_id, lot_id, "payment", payment_id, amount=amount, issued_by=issued_by)

    async def for_subscription(
        self, tenant_id: UUID, lot_id: UUID, subscription_id: UUID, *, amount: int, issued_by: UUID | None
    ) -> int:
        return await self._issue(tenant_id, lot_id, "subscription", subscription_id, amount=amount, issued_by=issued_by)

    async def _issue(
        self, tenant_id: UUID, lot_id: UUID, kind: str, source: UUID, *, amount: int, issued_by: UUID | None
    ) -> int:
        async with self._uow_factory(tenant_id) as uow:
            s = uow.session
            await s.execute(_LOCK_LOT, {"lot": lot_id})
            existing = (await s.execute(_FIND[kind], {"source": source})).first()
            if existing is not None:
                return int(existing.receipt_no)
            receipt_no = int((await s.execute(_NEXT, {"lot": lot_id})).scalar_one())
            await s.execute(
                _ADD[kind],
                {
                    "tenant": tenant_id,
                    "lot": lot_id,
                    "receipt_no": receipt_no,
                    "source": source,
                    "amount": amount,
                    "by": issued_by,
                },
            )
            return receipt_no
