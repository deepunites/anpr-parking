"""Бумажный чек об оплате парковки на принтере кассы (термопринтер 80 мм, ESC/POS по сети — Xprinter XP-H300M на
MediaPark). Не фискальный чек: номер машины, въезд, оплата, время стоянки, сумма и способ оплаты — то, что водитель
видит и уносит. Печатается после «Принять наличные» и повторно по кнопке; принтер не настроен или выключен — оплата
от этого не зависит, кассир видит ошибку печати."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from parking.modules.billing.domain.payment import Payment, PaymentStatus
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.parking.application.contracts import SessionView
from parking.modules.tenancy.application.contracts import LotView
from parking.shared.errors import ServiceUnavailableError, ValidationError
from parking.shared.money import Currency, Money

WIDTH = 48  # символов в строке 80-мм ленты шрифтом A
METHOD = {
    "CASH": "наличные",
    "CARD": "карта",
    "ONLINE": "онлайн",
    "SUBSCRIPTION": "абонемент",
    "VIP": "VIP",
    "WHITELIST": "белый список",
    "GRACE": "бесплатное время",
}


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    text: str = ""
    bold: bool = False
    center: bool = False
    big: bool = False  # двойная высота и ширина: строка вдвое короче


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
    receipt_no: str


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
    entry, paid = r.entry_time.astimezone(tz), r.paid_at.astimezone(tz)
    stay = int((r.paid_at - r.entry_time).total_seconds() // 60)
    lines = [
        ReceiptLine(r.lot_name, bold=True, center=True, big=True),
        ReceiptLine("Чек оплаты парковки", center=True),
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_pair("Номер:", _plate(r.plate)), bold=True),
        ReceiptLine(_pair("Въезд:", entry.strftime("%d.%m.%Y %H:%M"))),
        ReceiptLine(_pair("Оплата:", paid.strftime("%d.%m.%Y %H:%M"))),
        ReceiptLine(_pair("Время стоянки:", _duration(stay))),
    ]
    if r.billing_minutes is not None and r.billing_minutes != stay:
        lines.append(ReceiptLine(_pair("Оплачено время:", _duration(r.billing_minutes))))
    lines += [
        ReceiptLine("-" * WIDTH),
        ReceiptLine(_pair("ИТОГО:", r.amount.formatted("ru"), WIDTH // 2), bold=True, big=True),
        ReceiptLine(_pair("Оплата:", METHOD.get(r.method or "", (r.method or "—").lower()))),
        ReceiptLine(_pair("Чек №:", r.receipt_no)),
        ReceiptLine(),
        ReceiptLine("Спасибо! Rahmat!", center=True),
    ]
    return lines


class ReceiptService:
    def __init__(
        self,
        *,
        printer: ReceiptPrinter,
        payment_lookup: Callable[[CurrentUser, UUID], Awaitable[Payment]],
        session_lookup: Callable[[CurrentUser, UUID], Awaitable[SessionView]],
        lot_lookup: Callable[[CurrentUser, UUID], Awaitable[LotView]],
    ) -> None:
        self._printer = printer
        self._payment = payment_lookup
        self._session = session_lookup
        self._lot = lot_lookup

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
        return ReceiptData(
            lot_name=lot.name,
            timezone=lot.timezone,
            plate=session.plate,
            entry_time=session.entry_time,
            paid_at=payment.paid_at,
            billing_minutes=minutes if isinstance(minutes, int) else None,
            amount=Money(payment.amount, Currency(payment.currency)),
            method=payment.method.value if payment.method else None,
            receipt_no=str(payment.id)[:8].upper(),
        )

    async def print_payment(self, actor: CurrentUser, payment_id: UUID) -> ReceiptData:
        if not self._printer.configured:
            raise ServiceUnavailableError("receipt printer is not configured", code="PRINTER_NOT_CONFIGURED")
        data = await self.receipt(actor, payment_id)
        await self._printer.print(receipt_lines(data))
        return data
