"""Сетевой термопринтер чеков (ESC/POS, «raw» TCP 9100 — Xprinter XP-H300M и аналоги): кириллица — кодовая страница
PC866 (ESC t 17), отрезка ленты в конце. Принтер в сети площадки, адрес задаёт администратор сервера
(PARKING_RECEIPT_PRINTER=192.168.13.240:9100) — не пользователь панели, поэтому SSRF-проверки здесь нет."""

from __future__ import annotations

import asyncio

from parking.modules.billing.application.receipt import ReceiptLine
from parking.platform.logging import get_logger
from parking.shared.errors import ServiceUnavailableError

log = get_logger("billing.receipt")

ESC, GS, FS = b"\x1b", b"\x1d", b"\x1c"
LINGER_SECONDS = 1.0  # сетевой модуль принтера не успевает передать чек на печать, если соединение сразу закрыть
_PLAIN = str.maketrans({"—": "-", "–": "-", "«": '"', "»": '"', "‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", " ": " "})


def encode(text: str) -> bytes:
    """Строка для принтера: типографские знаки — в простые, остальное вне CP866 — «?»."""
    return text.translate(_PLAIN).encode("cp866", errors="replace")


QR_MODULE = 6  # точек на модуль: QR оплаты (~190 символов) — около 4 см на 80-мм ленте


def qr(data: str, module: int = QR_MODULE) -> bytes:
    """QR-код командами принтера (GS ( k, модель 2, коррекция M) по центру: строка банка как есть, без перекодировки."""
    payload = data.encode("ascii")
    size = len(payload) + 3
    return (
        ESC
        + b"a\x01"
        + GS
        + b"(k\x04\x00\x31\x41\x32\x00"  # модель 2
        + GS
        + b"(k\x03\x00\x31\x43"
        + bytes([module])  # размер модуля
        + GS
        + b"(k\x03\x00\x31\x45\x31"  # коррекция ошибок M
        + GS
        + b"(k"
        + bytes([size % 256, size // 256])
        + b"\x31\x50\x30"
        + payload  # данные
        + GS
        + b"(k\x03\x00\x31\x51\x30"  # печать
        + b"\n"
    )


def render(lines: list[ReceiptLine]) -> bytes:
    out = bytearray(ESC + b"@" + FS + b"." + ESC + b"t" + bytes([17]))  # сброс, без китайского режима, PC866
    for line in lines:
        if line.qr:
            out += qr(line.qr)
            continue
        out += ESC + b"a" + (b"\x01" if line.center else b"\x00")
        out += ESC + b"E" + (b"\x01" if line.bold else b"\x00")
        out += GS + b"!" + (b"\x22" if line.huge else b"\x11" if line.big else b"\x00")  # втрое / вдвое
        out += encode(line.text) + b"\n"
    out += GS + b"!" + b"\x00" + ESC + b"d" + bytes([4]) + GS + b"V" + b"\x42\x00"  # подача и отрезка
    return bytes(out)


class EscPosNetworkPrinter:
    def __init__(self, address: str, *, timeout: float = 5.0, linger: float = LINGER_SECONDS) -> None:
        host, _, port = address.strip().rpartition(":")
        self._host, self._port = (host, int(port)) if host and port.isdigit() else (address.strip(), 9100)
        self._timeout = timeout
        self._linger = linger

    @property
    def configured(self) -> bool:
        return bool(self._host)

    async def print(self, lines: list[ReceiptLine]) -> None:
        payload = render(lines)
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), self._timeout)
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), self._timeout)
            await asyncio.sleep(self._linger)
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), self._timeout)
        except (OSError, TimeoutError) as exc:
            log.warning("receipt_printer_unavailable", error_type=type(exc).__name__)
            raise ServiceUnavailableError("receipt printer is unavailable", code="PRINTER_UNAVAILABLE") from None


class NoPrinter:
    """Принтер не настроен — печать честно отказывает, оплата от этого не зависит."""

    configured = False

    async def print(self, lines: list[ReceiptLine]) -> None:
        raise ServiceUnavailableError("receipt printer is not configured", code="PRINTER_NOT_CONFIGURED")
