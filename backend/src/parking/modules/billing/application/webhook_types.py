"""Провайдеронезависимое представление входящего вызова провайдера (вебхук/колбэк протокола) и результата."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.billing.domain.payment import Provider


class Action(enum.StrEnum):
    CHECK = "CHECK"  # можно ли принять оплату по заказу (Payme CheckPerformTransaction, Click Prepare)
    CREATE = "CREATE"  # провайдер создал транзакцию (Payme CreateTransaction)
    PERFORM = "PERFORM"  # деньги списаны (Payme PerformTransaction, Click Complete)
    CANCEL = "CANCEL"  # провайдер отменил/вернул (Payme CancelTransaction, Click Complete с error<0)
    STATUS = "STATUS"  # запрос состояния (Payme CheckTransaction)
    STATEMENT = "STATEMENT"  # реестр за период (Payme GetStatement)


class Outcome(enum.StrEnum):
    ALLOW = "ALLOW"
    CREATED = "CREATED"
    PERFORMED = "PERFORMED"
    CANCELLED = "CANCELLED"
    STATUS = "STATUS"
    STATEMENT = "STATEMENT"
    SIGNATURE = "SIGNATURE"
    REPLAY = "REPLAY"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    TX_NOT_FOUND = "TX_NOT_FOUND"
    ALREADY_PAID = "ALREADY_PAID"
    ORDER_BUSY = "ORDER_BUSY"
    CANNOT_PERFORM = "CANNOT_PERFORM"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    MALFORMED = "MALFORMED"


ERROR_OUTCOMES = frozenset(
    {
        Outcome.SIGNATURE,
        Outcome.REPLAY,
        Outcome.ORDER_NOT_FOUND,
        Outcome.AMOUNT_MISMATCH,
        Outcome.TX_NOT_FOUND,
        Outcome.ALREADY_PAID,
        Outcome.ORDER_BUSY,
        Outcome.CANNOT_PERFORM,
        Outcome.UNKNOWN_ACTION,
        Outcome.MALFORMED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundEvent:
    provider: Provider
    merchant_id: str
    provider_event_id: str  # ключ дедупликации: один и тот же вызов дважды = один эффект
    action: Action
    provider_transaction_id: str | None = None
    payment_id: UUID | None = None  # наш заказ (account.order_id / merchant_trans_id)
    amount: int | None = None  # minor
    event_time: datetime | None = None
    rpc_id: Any = None
    payload: dict[str, Any] = field(default_factory=dict)  # без подписи и секретов — то, что кладём в inbox
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookResult:
    outcome: Outcome
    data: dict[str, Any] = field(default_factory=dict)  # create_time/perform_time/state/transaction/…
    payment_id: UUID | None = None

    @property
    def is_error(self) -> bool:
        return self.outcome in ERROR_OUTCOMES


class WebhookProtocol(Protocol):
    """Протокол провайдера (инфраструктура): разбор, подпись, рендер ответа — без бизнес-логики."""

    provider: Provider

    def parse(self, headers: dict[str, str], body: bytes, query: dict[str, str]) -> InboundEvent: ...
    def verify(self, event: InboundEvent, headers: dict[str, str], secret_json: str) -> bool: ...
    def render(self, event: InboundEvent, result: WebhookResult) -> tuple[int, dict[str, Any]]:
        """(HTTP-статус, тело) — ответ провайдеру по его протоколу."""
        ...
