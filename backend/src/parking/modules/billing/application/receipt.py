"""Бумажный чек об оплате парковки на принтере кассы (термопринтер 80 мм, ESC/POS по сети — Xprinter XP-H300M на
MediaPark). Не фискальный чек: номер машины, въезд, оплата, время стоянки, сумма и способ оплаты — то, что водитель
видит и уносит. Печатается по кнопке «Печать чека»; принтер не настроен или выключен — оплата от этого не зависит,
кассир видит ошибку печати.

Номер машины — крупно, втрое (MediaPark, 2026-10-19: «чтобы сразу бросался в глаза»). Чек оплаты — со сквозным
номером по площадке (реестр receipt: первая печать выдаёт номер, повторная — тот же). До оплаты — «Счёт к оплате»:
машина подъехала, кассир печатает сумму, номера у счёта нет."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from parking.modules.access.application.contracts import SubscriptionView
from parking.modules.billing.domain.payment import Payment, PaymentStatus
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.parking.application.contracts import SessionView
from parking.modules.tenancy.application.contracts import LotView
from parking.shared.clock import Clock
from parking.shared.errors import DomainError, ServiceUnavailableError, ValidationError
from parking.shared.money import Currency, Money

WIDTH = 48  # символов в строке 80-мм ленты шрифтом A
REPEAT_NOTE = "Повторный въезд за 24 ч: без бесплатных минут"
METHOD = {
    "CASH": "наличные",
    "CARD": "карта",
    "ONLINE": "онлайн",
    "SUBSCRIPTION": "абонемент",
    "VIP": "VIP",
    "WHITELIST": "белый список",
    "GRACE": "бесплатное время",
    "QR": "QR-код",
}


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    text: str = ""
    bold: bool = False
    center: bool = False
    big: bool = False  # двойная высота и ширина: строка вдвое короче
    huge: bool = False  # втрое: номер машины — сразу виден; строка втрое короче
    qr: str | None = None  # вместо текста — QR-код (оплата по QR площадки, не выходя из машины)


@dataclass(frozen=True, slots=True)
class ReceiptData:
    lot_name: str
    timezone: str
    plate: str | None
    entry_time: datetime
    paid_at: datetime
    billing_minutes: int | None
    amount: Money
    method: str | None
    receipt_no: str  # сквозной номер по площадке; у счёта до оплаты — пусто
    bill: bool = False  # «Счёт к оплате» до оплаты, а не чек оплаты
    note: str | None = None  # почему такая сумма, если не очевидно (повторный въезд за сутки)
    payment_qr: str | None = None  # статичный QR банка площадки — на счёте до оплаты


class ReceiptNumbers(Protocol):
    async def for_payment(
        self, tenant_id: UUID, lot_id: UUID, payment_id: UUID, *, amount: int, issued_by: UUID | None
    ) -> int:
        """Номер чека оплаты: выдаётся при первой печати (по площадке, без пропусков), дальше — тот же."""
        ...

    async def for_subscription(
        self, tenant_id: UUID, lot_id: UUID, subscription_id: UUID, *, amount: int, issued_by: UUID | None
    ) -> int: ...


class ReceiptPrinter(Protocol):
    @property
    def configured(self) -> bool: ...

    async def print(self, lines: list[ReceiptLine]) -> None: ...


def _pair(label: str, value: str, width: int = WIDTH) -> str:
    space = max(1, width - len(label) - len(value))
    return f"{label}{' ' * space}{value}"


def _duration(minutes: int) -> str:
    hours, rest = divmod(max(0, minutes), 60)
    return f"{hours} ч {rest:02d} мин" if hours else f"{rest} мин"


def _plate(plate: str | None) -> str:
    """01A123BC → 01 A 123 BC (как на табличке); иначе как есть."""
    if plate and len(plate) == 8 and plate[:2].isdigit() and plate[3:6].isdigit():
        return f"{plate[:2]} {plate[2]} {plate[3:6]} {plate[6:]}"
    return plate or "—"


def receipt_lines(r: ReceiptData) -> list[ReceiptLine]:
    tz = ZoneInfo(r.timezone)
    entry, at = r.entry_time.astimezone(tz), r.paid_at.astimezone(tz)
    stay = int((r.paid_at - r.entry_time).total_seconds() // 60)
    lines = [
        ReceiptLine(r.lot_name, bold=True, center=True, big=True),
        ReceiptLine("Счёт к оплате" if r.bill else "Чек оплаты парковки", center=True),
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_plate(r.plate), bold=True, center=True, huge=True),
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_pair("Въезд:", entry.strftime("%d.%m.%Y %H:%M"))),
        ReceiptLine(_pair("Сейчас:" if r.bill else "Оплата:", at.strftime("%d.%m.%Y %H:%M"))),
        ReceiptLine(_pair("Время стоянки:", _duration(stay))),
    ]
    # «Оплачено время» — только когда тариф округлил заметно (шаг 15/60 мин); 35 и 36 мин — округление секунд, путает
    if r.billing_minutes is not None and r.billing_minutes > stay + 1 and not r.bill:
        lines.append(ReceiptLine(_pair("Оплачено время:", _duration(r.billing_minutes))))
    if r.note:
        lines.append(ReceiptLine(r.note[:WIDTH]))
    lines += [
        ReceiptLine("-" * WIDTH),
        ReceiptLine(
            _pair("К ОПЛАТЕ:" if r.bill else "ИТОГО:", r.amount.formatted("ru"), WIDTH // 2), bold=True, big=True
        ),
    ]
    if r.bill and r.payment_qr:
        amount = r.amount.formatted("ru")
        lines += [
            ReceiptLine("-" * WIDTH),
            ReceiptLine("Оплата по QR, не выходя из машины", bold=True, center=True),
            ReceiptLine(qr=r.payment_qr),
            ReceiptLine(f"Сумма: {amount}", bold=True, center=True),
            ReceiptLine("Отсканируйте QR в приложении банка,", center=True),
            ReceiptLine("введите сумму и покажите оплату кассиру", center=True),
            ReceiptLine("QR orqali to'lang: summani kiriting", center=True),
            ReceiptLine(),
            ReceiptLine("Или наличными на кассе", center=True),
        ]
    elif r.bill:
        lines += [ReceiptLine(), ReceiptLine("Оплатите на кассе", center=True)]
    else:
        lines += [
            ReceiptLine(_pair("Оплата:", METHOD.get(r.method or "", (r.method or "—").lower()))),
            ReceiptLine(_pair("Чек №:", r.receipt_no), bold=True),
            ReceiptLine(),
            ReceiptLine("Спасибо! Rahmat!", center=True),
        ]
    return lines


def subscription_lines(lot_name: str, timezone: str, sub: SubscriptionView, receipt_no: str) -> list[ReceiptLine]:
    """Чек месячного абонемента: весь срок въезд и выезд бесплатно (MediaPark, 2026-10-19)."""
    tz = ZoneInfo(timezone)
    starts, ends = sub.starts_at.astimezone(tz), sub.ends_at.astimezone(tz)
    return [
        ReceiptLine(lot_name, bold=True, center=True, big=True),
        ReceiptLine("Абонемент на стоянку", center=True),
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_plate(sub.plate), bold=True, center=True, huge=True),
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_pair("Действует с:", starts.strftime("%d.%m.%Y %H:%M"))),
        ReceiptLine(_pair("Действует до:", ends.strftime("%d.%m.%Y %H:%M")), bold=True),
        ReceiptLine("Въезд и выезд весь срок — бесплатно", center=True),
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_pair("ИТОГО:", Money(sub.price_minor).formatted("ru"), WIDTH // 2), bold=True, big=True),
        ReceiptLine(_pair("Оплата:", METHOD["CASH"])),
        ReceiptLine(_pair("Чек №:", receipt_no), bold=True),
        ReceiptLine(),
        ReceiptLine("Спасибо! Rahmat!", center=True),
    ]


class ReceiptService:
    def __init__(
        self,
        *,
        printer: ReceiptPrinter,
        payment_lookup: Callable[[CurrentUser, UUID], Awaitable[Payment]],
        session_lookup: Callable[[CurrentUser, UUID], Awaitable[SessionView]],
        lot_lookup: Callable[[CurrentUser, UUID], Awaitable[LotView]],
        numbers: ReceiptNumbers,
        session_payments: Callable[[CurrentUser, UUID], Awaitable[list[Payment]]],
        quote: Callable[[CurrentUser, UUID], Awaitable[int]],
        clock: Clock,
        subscription_lookup: Callable[[CurrentUser, UUID], Awaitable[SubscriptionView]] | None = None,
    ) -> None:
        self._printer = printer
        self._payment = payment_lookup
        self._session = session_lookup
        self._lot = lot_lookup
        self._numbers = numbers
        self._session_payments = session_payments
        self._quote = quote
        self._clock = clock
        self._subscription = subscription_lookup

    @property
    def configured(self) -> bool:
        return self._printer.configured

    async def receipt(self, actor: CurrentUser, payment_id: UUID) -> ReceiptData:
        payment = await self._payment(actor, payment_id)  # 404, если платёж чужой или вне скоупа площадок
        if payment.status is not PaymentStatus.PAID or payment.paid_at is None:
            raise ValidationError("receipt is printed only for a paid payment", code="PAYMENT_NOT_PAID")
        session = await self._session(actor, payment.session_id)
        lot = await self._lot(actor, payment.lot_id)
        minutes = payment.calculation_snapshot.get("billing_minutes")
        applied = payment.calculation_snapshot.get("applied_rules") or []
        note = REPEAT_NOTE if "REPEAT_VISIT" in applied else None
        number = await self._numbers.for_payment(
            actor.tenant_id, payment.lot_id, payment.id, amount=payment.amount, issued_by=actor.user_id
        )
        return ReceiptData(
            lot_name=lot.name,
            timezone=lot.timezone,
            plate=session.plate,
            entry_time=session.entry_time,
            paid_at=payment.paid_at,
            billing_minutes=minutes if isinstance(minutes, int) else None,
            amount=Money(payment.amount, Currency(payment.currency)),
            method=payment.method.value if payment.method else None,
            receipt_no=f"{number:06d}",
            note=note,
        )

    async def bill(self, actor: CurrentUser, session_id: UUID) -> ReceiptData:
        """Счёт до оплаты: машина у выезда — сумма выезда (обязательство саги) или сколько должна сейчас."""
        session = await self._session(actor, session_id)  # 404, если сессия чужая или вне скоупа площадок
        lot = await self._lot(actor, session.lot_id)
        payments = await self._session_payments(actor, session_id)
        pending = next((p for p in payments if p.status is PaymentStatus.PENDING), None)
        if pending is not None:
            amount = pending.amount
        else:
            try:
                amount = await self._quote(actor, session_id)
            except DomainError:
                raise ValidationError("nothing to pay for this session", code="NOTHING_TO_PAY") from None
        return ReceiptData(
            lot_name=lot.name,
            timezone=lot.timezone,
            plate=session.plate,
            entry_time=session.entry_time,
            paid_at=self._clock.now(),
            billing_minutes=None,
            amount=Money(amount),
            method=None,
            receipt_no="",
            bill=True,
            payment_qr=lot.policies.payment_qr,
        )

    async def print_subscription(self, actor: CurrentUser, subscription_id: UUID, lot_id: UUID) -> tuple[str, Money]:
        """Чек проданного абонемента: номер выдаётся один раз (повторная печать — тот же); площадка — абонемента,
        а у абонемента на все площадки — та, где продали."""
        if self._subscription is None:
            raise ServiceUnavailableError("subscriptions are not available")
        sub = await self._subscription(actor, subscription_id)  # 404, если чужой или вне скоупа площадок
        lot = await self._lot(actor, sub.lot_id or lot_id)
        self._require_printer()
        number = await self._numbers.for_subscription(
            actor.tenant_id, lot.id, sub.id, amount=sub.price_minor, issued_by=actor.user_id
        )
        receipt_no = f"{number:06d}"
        await self._printer.print(subscription_lines(lot.name, lot.timezone, sub, receipt_no))
        return receipt_no, Money(sub.price_minor)

    async def print_bill(self, actor: CurrentUser, session_id: UUID) -> ReceiptData:
        data = await self.bill(actor, session_id)  # 404 чужой сессии раньше ответа про принтер
        self._require_printer()
        await self._printer.print(receipt_lines(data))
        return data

    async def print_payment(self, actor: CurrentUser, payment_id: UUID) -> ReceiptData:
        await self._payment(actor, payment_id)  # сначала 404 чужому платежу, потом — про принтер
        self._require_printer()
        data = await self.receipt(actor, payment_id)
        await self._printer.print(receipt_lines(data))
        return data

    def _require_printer(self) -> None:
        if not self._printer.configured:
            raise ServiceUnavailableError("receipt printer is not configured", code="PRINTER_NOT_CONFIGURED")
