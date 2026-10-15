"""Бумажный чек оплаты на сетевом принтере кассы (ESC/POS, порт 9100): только оплаченный платёж, кириллица в PC866,
лента отрезается; строки влезают в 80 мм; принтер выключен — понятная ошибка, а не падение оплаты."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from parking.modules.billing.application.receipt import WIDTH, ReceiptData, ReceiptService, receipt_lines
from parking.modules.billing.infrastructure.escpos import EscPosNetworkPrinter, render
from parking.modules.identity.application.contracts import CurrentUser
from parking.runtime.container import Container
from parking.shared.errors import ServiceUnavailableError, ValidationError
from parking.shared.money import Money
from parking.shared.plate import PlateNumber
from tests.support.billing_fixtures import make_obligation


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
        receipt_no="AB12CD34",
    )
    lines = receipt_lines(data)
    text = "\n".join(line.text for line in lines)

    assert all(len(line.text) <= (WIDTH // 2 if line.big else WIDTH) for line in lines)
    assert "01 A 123 BC" in text and "15.10.2026 12:03" in text and "15.10.2026 13:10" in text
    assert "1 ч 07 мин" in text and "2 ч 00 мин" in text  # стоял 1:07, оплачено два начатых часа
    assert "13 000,00 сум" in text and "наличные" in text and "AB12CD34" in text
    payload = render(lines)
    assert payload.startswith(b"\x1b@\x1bt\x11") and payload.endswith(b"\x1dVB\x00")  # PC866, отрезка
    assert "Чек оплаты парковки".encode("cp866") in payload
    free = receipt_lines(dataclasses.replace(data, amount=Money(0), method="GRACE"))
    assert any("бесплатное время" in line.text for line in free)


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
        printer=EscPosNetworkPrinter(f"127.0.0.1:{port}"),
        payment_lookup=container.cashier.get_payment,
        session_lookup=container.parking.get_session,
        lot_lookup=container.lots.get,
    )
    ob = await make_obligation(container, owner, PlateNumber.parse(f"01R{uuid4().int % 900 + 100}RR"))

    with pytest.raises(ValidationError):
        await receipts.print_payment(cashier, ob.payment_id)  # ещё не оплачен — чека нет
    await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=ob.amount, reason=None)
    printed = await receipts.print_payment(cashier, ob.payment_id)

    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)
    text = received[0].decode("cp866", errors="ignore")
    assert "Чек оплаты парковки" in text and printed.amount.formatted("ru") in text and "наличные" in text
    assert printed.receipt_no in text

    server.close()
    await server.wait_closed()
    with pytest.raises(ServiceUnavailableError):  # принтер выключен
        await receipts.print_payment(cashier, ob.payment_id)
