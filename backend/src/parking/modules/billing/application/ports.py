from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.billing.domain.payment import (
    CashierShift,
    Payment,
    PaymentAttempt,
    PaymentStatus,
    Provider,
    Refund,
)
from parking.modules.billing.domain.reconciliation import Discrepancy, DiscrepancyStatus, RemoteState, RemoteTx
from parking.shared.errors import ExternalServiceError


class PaymentRepository(Protocol):
    async def get(self, payment_id: UUID, *, for_update: bool = False) -> Payment | None: ...
    async def find_by_key(self, idempotency_key: bytes) -> Payment | None: ...
    async def list_for_session(self, session_id: UUID) -> list[Payment]: ...
    async def pending_for_session(self, session_id: UUID, *, for_update: bool = False) -> Payment | None: ...
    async def add(self, payment: Payment) -> None: ...
    async def save(self, payment: Payment) -> None:
        """UPDATE под триггером app.payment_guard (I-6): терминальный платёж БД не даст изменить."""
        ...

    async def settled_for_provider(
        self, provider: Provider, start: date, end: date
    ) -> list[tuple[Payment, str | None]]:
        """PAID/REFUNDED платежи провайдера за период с provider_transaction_id из попытки — для сверки."""
        ...

    async def session_lot(self, session_id: UUID) -> UUID | None: ...


class AttemptRepository(Protocol):
    async def add(self, attempt: PaymentAttempt) -> None: ...
    async def save(self, attempt: PaymentAttempt) -> None: ...
    async def get(self, attempt_id: UUID, *, for_update: bool = False) -> PaymentAttempt | None: ...
    async def next_no(self, payment_id: UUID) -> int: ...
    async def latest(self, payment_id: UUID) -> PaymentAttempt | None: ...
    async def find_by_provider_tx(self, provider: Provider, provider_transaction_id: str) -> PaymentAttempt | None: ...
    async def unknown(self, *, limit: int) -> list[PaymentAttempt]:
        """Попытки UNKNOWN/TIMEOUT — джоб верификации спрашивает status(), не создаёт заново (§1 п.4)."""
        ...

    async def list_between(self, provider: Provider, start: datetime, end: datetime) -> list[PaymentAttempt]: ...


class RefundRepository(Protocol):
    async def add(self, refund: Refund) -> None: ...
    async def save(self, refund: Refund) -> None: ...
    async def get(self, refund_id: UUID, *, for_update: bool = False) -> Refund | None: ...
    async def find_by_key(self, idempotency_key: bytes) -> Refund | None: ...
    async def list_for_payment(self, payment_id: UUID) -> list[Refund]: ...
    async def pending_provider(self, *, limit: int) -> list[Refund]: ...


class ShiftRepository(Protocol):
    async def add(self, shift: CashierShift) -> None: ...
    async def get(self, shift_id: UUID, *, for_update: bool = False) -> CashierShift | None: ...
    async def open_for_cashier(self, cashier_id: UUID, *, for_update: bool = False) -> CashierShift | None: ...
    async def save(self, shift: CashierShift) -> None: ...
    async def cash_totals(self, shift_id: UUID) -> tuple[int, int]:
        """(наличные платежи PAID, наличные возвраты DONE) смены."""
        ...

    async def list(self, *, lot_ids: frozenset[UUID] | None, limit: int) -> list[CashierShift]: ...


class WebhookStatus:
    NEW = "NEW"
    APPLIED = "APPLIED"
    ORPHAN = "ORPHAN"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


@dataclass(slots=True, kw_only=True)
class InboxRecord:
    id: UUID
    tenant_id: UUID
    provider: Provider
    provider_event_id: str
    payload: dict[str, Any]
    signature_valid: bool
    provider_transaction_id: str | None = None
    status: str = WebhookStatus.NEW
    applied_payment_id: UUID | None = None
    error: str | None = None
    response: dict[str, Any] | None = None
    attempts: int = 0


class WebhookInboxRepository(Protocol):
    async def add(self, record: InboxRecord) -> InboxRecord | None:
        """INSERT … ON CONFLICT DO NOTHING; при дубле возвращает уже сохранённую запись (с её ответом)."""
        ...

    async def save(self, record: InboxRecord) -> None: ...
    async def orphans(self, *, limit: int) -> list[InboxRecord]: ...


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    id: UUID
    tenant_id: UUID
    provider: Provider
    merchant_id: str
    secret_ref: str
    active: bool


class ProviderAccountLookup(Protocol):
    async def by_merchant(self, provider: Provider, merchant_id: str) -> ProviderAccount | None:
        """Pre-tenant (SECURITY DEFINER): вебхук приходит до того, как известен tenant."""
        ...

    async def for_tenant(self, provider: Provider) -> ProviderAccount | None:
        """Внутри tenant-контекста."""
        ...


@dataclass(slots=True, kw_only=True)
class ReconciliationRun:
    id: UUID
    tenant_id: UUID
    provider: Provider
    period_start: date
    period_end: date
    started_at: datetime
    finished_at: datetime | None = None
    local_count: int = 0
    remote_count: int = 0
    matched_count: int = 0
    discrepancy_count: int = 0
    report: dict[str, Any] = field(default_factory=dict)
    created_by: UUID | None = None


@dataclass(slots=True, kw_only=True)
class DiscrepancyRecord:
    id: UUID
    tenant_id: UUID
    reconciliation_id: UUID
    discrepancy: Discrepancy
    status: DiscrepancyStatus = DiscrepancyStatus.OPEN
    assigned_to: UUID | None = None
    resolution: str | None = None
    resolved_by: UUID | None = None
    resolved_at: datetime | None = None


class ReconciliationRepository(Protocol):
    async def add_run(self, run: ReconciliationRun) -> None: ...
    async def save_run(self, run: ReconciliationRun) -> None: ...
    async def get_run(self, run_id: UUID) -> ReconciliationRun | None: ...
    async def add_discrepancy(self, record: DiscrepancyRecord) -> None: ...
    async def get_discrepancy(self, record_id: UUID, *, for_update: bool = False) -> DiscrepancyRecord | None: ...
    async def save_discrepancy(self, record: DiscrepancyRecord) -> None: ...
    async def list_discrepancies(self, *, status: DiscrepancyStatus | None, limit: int) -> list[DiscrepancyRecord]: ...
    async def open_count(self) -> int: ...


# ---------- шлюз провайдера ----------


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    payment_id: UUID
    session_id: UUID
    amount: int
    currency: str
    idempotency_key: str  # устойчив между попытками: повтор create() с тем же ключом — тот же intent у провайдера
    description: str
    return_url: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    provider_transaction_id: str | None
    checkout_url: str | None
    state: RemoteState
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentStatusResult:
    state: RemoteState | None  # None — провайдер не знает такой транзакции/ключа
    provider_transaction_id: str | None
    amount: int | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefundRequest:
    provider_transaction_id: str
    amount: int
    idempotency_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class RefundResult:
    ok: bool
    provider_refund_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(Protocol):
    """Все вызовы — вне транзакций (I-9), с таймаутом, breaker'ом и метриками; секреты не логируются."""

    async def create(self, req: PaymentRequest) -> PaymentIntent: ...
    async def status(self, provider_tx_id: str | None, *, idempotency_key: str) -> PaymentStatusResult: ...
    async def refund(self, req: RefundRequest) -> RefundResult: ...
    async def registry(self, start: date, end: date) -> list[RemoteTx]:
        """Реестр провайдера за период — для сверки (§5)."""
        ...


class GatewayRegistry(Protocol):
    def gateway_for(self, account: ProviderAccount) -> PaymentGateway: ...


class GatewayError(ExternalServiceError):
    """Провайдер ответил ошибкой или недоступен (HTTP 502 в API); сообщение — без секретов."""


class GatewayTimeoutError(GatewayError):
    """Ответ не получен: результат неизвестен — ретрай create() запрещён, только status()."""


__all__ = [
    "AttemptRepository",
    "DiscrepancyRecord",
    "GatewayError",
    "GatewayRegistry",
    "GatewayTimeoutError",
    "InboxRecord",
    "PaymentGateway",
    "PaymentIntent",
    "PaymentRepository",
    "PaymentRequest",
    "PaymentStatus",
    "PaymentStatusResult",
    "ProviderAccount",
    "ProviderAccountLookup",
    "ReconciliationRepository",
    "ReconciliationRun",
    "RefundRepository",
    "RefundRequest",
    "RefundResult",
    "ShiftRepository",
    "WebhookInboxRepository",
    "WebhookStatus",
]
