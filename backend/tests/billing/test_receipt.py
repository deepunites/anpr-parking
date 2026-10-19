"""Бумажный чек оплаты на сетевом принтере кассы (ESC/POS, порт 9100): только оплаченный платёж, кириллица в PC866,
лента отрезается; строки влезают в 80 мм; принтер выключен — понятная ошибка, а не падение оплаты.
Номер машины — втрое крупнее; сквозной номер чека по площадке (повторная печать — тот же); счёт до оплаты — без номера."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from parking.modules.access.application.admin_service import CreateSubscriptionCommand
from parking.modules.billing.application.receipt import REPEAT_NOTE, WIDTH, ReceiptData, ReceiptService, receipt_lines
from parking.modules.billing.domain.payment import PaymentMethod, PaymentStatus
from parking.modules.billing.infrastructure.escpos import EscPosNetworkPrinter, render
from parking.modules.billing.infrastructure.receipts import SqlReceiptNumbers
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.runtime.container import Container
from parking.shared.errors import ServiceUnavailableError, ValidationError
from parking.shared.money import Money
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.billing_fixtures import make_obligation

QR = "00020101021140440012qr-online.uz0118TESTMERCHANT00000102020152045814530386058" + "02UZ5904TEST6304ABCD"


def test_receipt_layout_fits_80mm_and_reads_like_a_receipt() -> None:
    entry = datetime(2026, 10, 15, 7, 3, tzinfo=UTC)  # 12:03 по Ташкенту
    data = ReceiptData(
        lot_name="MediaPark",
        timezone="Asia/Tashkent",
        plate="01A123BC",
        entry_time=entry,
        paid_at=entry + timedelta(hours=1, minutes=7),
        billing_minutes=120,
        amount=Money(1_300_000),
        method="CASH",
        receipt_no="000123",
    )
    lines = receipt_lines(data)
    text = "\n".join(line.text for line in lines)

    assert all(len(line.text) <= (WIDTH // 3 if line.huge else WIDTH // 2 if line.big else WIDTH) for line in lines)
    plate = next(line for line in lines if line.text == "01 A 123 BC")
    assert plate.huge and plate.center and plate.bold, "номер машины — крупно, по центру (MediaPark)"
    assert "01 A 123 BC" in text and "15.10.2026 12:03" in text and "15.10.2026 13:10" in text
    assert "1 ч 07 мин" in text and "2 ч 00 мин" in text  # стоял 1:07, оплачено два начатых часа
    assert "13 000,00 сум" in text and "наличные" in text and "000123" in text
    payload = render(lines)
    assert payload.startswith(b"\x1b@\x1c.\x1bt\x11") and payload.endswith(b"\x1dVB\x00")  # PC866, отрезка
    assert b"\x1d!\x22" + "01 A 123 BC".encode("cp866") in payload, "втрое: GS ! 0x22"
    assert "Чек оплаты парковки".encode("cp866") in payload
    seconds = "\n".join(line.text for line in receipt_lines(dataclasses.replace(data, billing_minutes=68)))
    assert "Оплачено время" not in seconds, "стоял 1:07, оплачено 1:08 — округление секунд, строка лишняя"
    free = receipt_lines(dataclasses.replace(data, amount=Money(0), method="GRACE"))
    assert any("бесплатное время" in line.text for line in free)
    bill = "\n".join(
        line.text for line in receipt_lines(dataclasses.replace(data, bill=True, receipt_no="", method=None))
    )
    assert "Счёт к оплате" in bill and "К ОПЛАТЕ" in bill and "13 000,00 сум" in bill
    assert "Чек №" not in bill and "наличные" not in bill, "у счёта нет номера и способа оплаты"
    qr_bill = receipt_lines(dataclasses.replace(data, bill=True, receipt_no="", method=None, payment_qr=QR))
    assert [line.qr for line in qr_bill if line.qr] == [QR], "QR площадки — на счёте до оплаты"
    assert any("Сумма: 13 000,00 сум" in line.text for line in qr_bill)
    assert all(len(line.text) <= (WIDTH // 2 if line.big else WIDTH) for line in qr_bill if not line.huge)
    printed = render(qr_bill)
    assert b"\x1d(k" in printed and QR.encode() in printed, "QR командой принтера, строка банка как есть"
    assert not any(line.qr for line in lines), "на чеке оплаты QR не нужен"
    repeat = "\n".join(line.text for line in receipt_lines(dataclasses.replace(data, note=REPEAT_NOTE)))
    assert REPEAT_NOTE in repeat and len(REPEAT_NOTE) <= WIDTH


async def test_paid_payment_is_printed_on_network_printer(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    received: list[bytes] = []

    async def printer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read())
        writer.close()

    server = await asyncio.start_server(printer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    receipts = ReceiptService(
        printer=EscPosNetworkPrinter(f"127.0.0.1:{port}", linger=0.0),
        payment_lookup=container.cashier.get_payment,
        session_lookup=container.parking.get_session,
        lot_lookup=container.lots.get,
        numbers=SqlReceiptNumbers(container.uow),
        session_payments=container.cashier.session_payments,
        quote=container.cashier.quote,
        clock=container.clock,
    )
    ob = await make_obligation(container, owner, PlateNumber.parse(f"01R{uuid4().int % 900 + 100}RR"))

    with pytest.raises(ValidationError):
        await receipts.print_payment(cashier, ob.payment_id)  # ещё не оплачен — чека нет
    bill = await receipts.print_bill(cashier, ob.session_id)  # машина у выезда: счёт до оплаты
    assert bill.amount.minor == ob.amount and bill.receipt_no == ""
    await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=ob.amount, reason=None)
    printed = await receipts.print_payment(cashier, ob.payment_id)
    again = await receipts.print_payment(cashier, ob.payment_id)
    other = await make_obligation(container, owner, PlateNumber.parse(f"01S{uuid4().int % 900 + 100}SS"))
    await container.cashier.confirm_cash(cashier, other.payment_id, declared_amount=other.amount, reason=None)
    second = await receipts.print_payment(cashier, other.payment_id)

    assert printed.receipt_no.isdigit() and again.receipt_no == printed.receipt_no, "повторная печать — тот же номер"
    assert int(second.receipt_no) == int(printed.receipt_no) + 1, "сквозная нумерация по площадке"
    for _ in range(50):
        if len(received) >= 4:
            break
        await asyncio.sleep(0.05)
    texts = [r.decode("cp866", errors="ignore") for r in received]
    assert "Счёт к оплате" in texts[0] and "Чек №" not in texts[0]
    assert "Чек оплаты парковки" in texts[1] and printed.amount.formatted("ru") in texts[1] and "наличные" in texts[1]
    assert printed.receipt_no in texts[1]

    server.close()
    await server.wait_closed()
    with pytest.raises(ServiceUnavailableError):  # принтер выключен
        await receipts.print_payment(cashier, ob.payment_id)


async def test_subscription_sale_prints_numbered_receipt(container: Container) -> None:
    """Месячный абонемент (MediaPark, 2026-10-19): продаёт администратор, чек — номер машины крупно, срок, сумма,
    сквозной номер чека (тот же реестр, что у чеков оплаты); повторная печать — тот же номер."""
    received: list[bytes] = []

    async def printer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read())
        writer.close()

    server = await asyncio.start_server(printer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    receipts = ReceiptService(
        printer=EscPosNetworkPrinter(f"127.0.0.1:{port}", linger=0.0),
        payment_lookup=container.cashier.get_payment,
        session_lookup=container.parking.get_session,
        lot_lookup=container.lots.get,
        numbers=SqlReceiptNumbers(container.uow),
        session_payments=container.cashier.session_payments,
        quote=container.cashier.quote,
        clock=container.clock,
        subscription_lookup=container.access.subscription,
    )
    admin = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
    plate = f"01M{uuid4().int % 900 + 100}MM"
    now = datetime.now(tz=UTC)
    rule = await container.access.create_subscription(
        admin,
        CreateSubscriptionCommand(
            plate=plate,
            lot_id=None,
            plan_code="M1",
            starts_at=now,
            ends_at=now + timedelta(days=30),
            price_minor=30000000,
        ),
        reason="абонемент на месяц",
    )
    assert rule.source_id is not None

    number, amount = await receipts.print_subscription(admin, rule.source_id, LOT_A1)
    again, _ = await receipts.print_subscription(admin, rule.source_id, LOT_A1)

    assert number.isdigit() and again == number and amount.minor == 30000000
    for _ in range(50):
        if len(received) >= 2:
            break
        await asyncio.sleep(0.05)
    text = received[0].decode("cp866", errors="ignore")
    assert "Абонемент на стоянку" in text and "Действует до" in text and number in text
    assert f"{plate[:2]} {plate[2]} {plate[3:6]} {plate[6:]}" in text and "300 000,00 сум" in text
    server.close()
    await server.wait_closed()


async def test_qr_payment_is_confirmed_by_cashier_and_kept_out_of_cash_drawer(
    container: Container, owner: asyncpg.Connection, cashier: CurrentUser, clean_lot: None
) -> None:
    """Оплата по QR площадки (MediaPark, 2026-10-19): водитель платит из машины, кассир видит поступление и жмёт
    «Оплачено по QR» — платёж PAID способом QR, стрела открывается сагой; в наличные смены сумма не входит."""
    ob = await make_obligation(container, owner, PlateNumber.parse(f"01Q{uuid4().int % 900 + 100}QQ"))
    paid = await container.cashier.confirm_cash(
        cashier, ob.payment_id, declared_amount=ob.amount, reason=None, method=PaymentMethod.QR
    )
    assert paid.status is PaymentStatus.PAID and paid.method is PaymentMethod.QR and paid.shift_id is not None
    cash_in = await owner.fetchval(
        "SELECT coalesce(sum(amount), 0) FROM payment WHERE shift_id = $1 AND method = 'CASH' AND id = $2",
        paid.shift_id,
        paid.id,
    )
    assert cash_in == 0, "QR не в кассе смены"
    again = await container.cashier.confirm_cash(
        cashier, ob.payment_id, declared_amount=ob.amount, reason=None, method=PaymentMethod.QR
    )
    assert again.id == paid.id, "двойное нажатие — одна оплата"
