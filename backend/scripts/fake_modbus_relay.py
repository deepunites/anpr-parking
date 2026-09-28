"""Имитатор сетевого Modbus TCP реле со шлагбаумом (как Waveshare Modbus POE ETH Relay + шлагбаум APOLLO).

Реле: FC01 Read Coils, FC05 Write Single Coil, FC0F Write Multiple Coils; прочее — исключение 01.
Шлагбаум на реле: импульс на катушке open_coil (0) — стрела поднимается (1,5 с), стоит поднятой auto_close секунд
и опускается (как автозакрытие платы); импульс на close_coil (1) — опускается сразу.
Страница состояния: http://<host>:<http_port>/ (стрела, катушки, журнал), JSON — /state,
отказы для проверки поведения системы: POST /mode {"mode": "ok" | "refuse" | "timeout" | "exception"}.

    python scripts/fake_modbus_relay.py [--host 0.0.0.0] [--port 1502] [--http-port 8098] [--auto-close 5]

В системе: драйвер «Modbus TCP», адрес <IP>:1502?unit=1&open_coil=0&pulse_ms=500, подсеть IP — в «Подсетях
контроллеров стрел» площадки (127.0.0.1 не подойдёт: SSRF-политика запрещает loopback).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime

MODES = ("ok", "refuse", "timeout", "exception")
TRAVEL_SECONDS = 1.5


@dataclass
class Boom:
    state: str = "CLOSED"  # CLOSED | OPENING | OPEN | CLOSING
    since: float = field(default_factory=time.monotonic)
    opens: int = 0

    def advance(self, auto_close: float) -> None:
        elapsed = time.monotonic() - self.since
        if self.state == "OPENING" and elapsed >= TRAVEL_SECONDS:
            self._set("OPEN")
        elif self.state == "OPEN" and elapsed >= auto_close:
            self._set("CLOSING")
        elif self.state == "CLOSING" and elapsed >= TRAVEL_SECONDS:
            self._set("CLOSED")

    def _set(self, state: str) -> None:
        self.state, self.since = state, time.monotonic()

    def command_open(self) -> None:
        if self.state in ("CLOSED", "CLOSING"):
            self._set("OPENING")
            self.opens += 1
        elif self.state == "OPEN":
            self.since = time.monotonic()  # повторный импульс продлевает автозакрытие

    def command_close(self) -> None:
        if self.state in ("OPEN", "OPENING"):
            self._set("CLOSING")


class RelaySimulator:
    def __init__(self, *, coils: int = 8, open_coil: int = 0, close_coil: int = 1, auto_close: float = 5.0) -> None:
        self.coils = [False] * coils
        self.open_coil, self.close_coil = open_coil, close_coil
        self.auto_close = auto_close
        self.boom = Boom()
        self.mode = "ok"
        self.journal: list[dict[str, object]] = []
        self.requests = 0

    # ---------- модель ----------
    def _log(self, event: str, **extra: object) -> None:
        self.journal.append({"at": datetime.now().strftime("%H:%M:%S.%f")[:-3], "event": event, **extra})
        del self.journal[:-50]

    def _write_coil(self, index: int, value: bool, peer: str) -> None:
        before = self.coils[index]
        self.coils[index] = value
        if value and not before:  # передний фронт — как нажатие кнопки на плате
            if index == self.open_coil:
                self.boom.command_open()
                self._log("OPEN", coil=index, peer=peer)
            elif index == self.close_coil:
                self.boom.command_close()
                self._log("CLOSE", coil=index, peer=peer)
            else:
                self._log("COIL_ON", coil=index, peer=peer)

    def state(self) -> dict[str, object]:
        self.boom.advance(self.auto_close)
        return {
            "boom": self.boom.state,
            "opens": self.boom.opens,
            "coils": [int(c) for c in self.coils],
            "mode": self.mode,
            "requests": self.requests,
            "auto_close_seconds": self.auto_close,
            "journal": list(reversed(self.journal)),
        }

    # ---------- Modbus TCP ----------
    async def handle_modbus(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = str(writer.get_extra_info("peername", ("?",))[0])
        try:
            while True:
                header = await reader.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                self.requests += 1
                if self.mode == "refuse":
                    self._log("REFUSED", peer=peer)
                    return
                if self.mode == "timeout":
                    self._log("NO_REPLY", peer=peer)
                    await asyncio.sleep(3600)
                    return
                reply = self._reply(pdu, peer)
                writer.write(struct.pack(">HHHB", tid, proto, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def _reply(self, pdu: bytes, peer: str) -> bytes:  # noqa: PLR0911 — таблица функций Modbus
        fc = pdu[0]
        if self.mode == "exception":
            return bytes([fc | 0x80, 0x04])  # SERVER DEVICE FAILURE
        if fc == 0x01:  # Read Coils
            start, count = struct.unpack(">HH", pdu[1:5])
            if start + count > len(self.coils) or count == 0:
                return bytes([0x81, 0x02])
            nbytes = (count + 7) // 8
            packed = bytearray(nbytes)
            for i in range(count):
                if self.coils[start + i]:
                    packed[i // 8] |= 1 << (i % 8)
            return bytes([0x01, nbytes]) + bytes(packed)
        if fc == 0x05:  # Write Single Coil — ответ эхом
            addr, value = struct.unpack(">HH", pdu[1:5])
            if addr >= len(self.coils) or value not in (0x0000, 0xFF00):
                return bytes([0x85, 0x02 if addr >= len(self.coils) else 0x03])
            self._write_coil(addr, value == 0xFF00, peer)
            return pdu[:5]
        if fc == 0x0F:  # Write Multiple Coils
            start, count, _nbytes = struct.unpack(">HHB", pdu[1:6])
            if start + count > len(self.coils):
                return bytes([0x8F, 0x02])
            for i in range(count):
                self._write_coil(start + i, bool(pdu[6 + i // 8] >> (i % 8) & 1), peer)
            return pdu[:5]
        return bytes([fc | 0x80, 0x01])  # ILLEGAL FUNCTION

    # ---------- HTTP (страница состояния) ----------
    async def handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            method, path, *_ = request.decode("latin-1").split(" ", 2)
            length = 0
            for line in request.decode("latin-1").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            body = await reader.readexactly(length) if length else b""
            if method == "GET" and path.startswith("/state"):
                self._send(writer, 200, "application/json", json.dumps(self.state(), ensure_ascii=False))
            elif method == "POST" and path.startswith("/mode"):
                mode = json.loads(body or b"{}").get("mode", "ok")
                if mode not in MODES:
                    self._send(writer, 400, "application/json", json.dumps({"error": f"mode: {MODES}"}))
                else:
                    self.mode = mode
                    self._log("MODE", mode=mode)
                    self._send(writer, 200, "application/json", json.dumps({"mode": mode}))
            elif method == "GET" and path in ("/", "/index.html"):
                self._send(writer, 200, "text/html; charset=utf-8", PAGE)
            else:
                self._send(writer, 404, "text/plain", "not found")
            await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _send(writer: asyncio.StreamWriter, status: int, ctype: str, text: str) -> None:
        raw = text.encode()
        head = (
            f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode() + raw)

    async def serve(self, host: str, port: int, http_port: int | None) -> None:
        servers = [await asyncio.start_server(self.handle_modbus, host, port)]
        if http_port:
            servers.append(await asyncio.start_server(self.handle_http, host, http_port))
        print(f"modbus relay simulator: tcp {host}:{port}, status http://{host}:{http_port}/", flush=True)
        await asyncio.gather(*(s.serve_forever() for s in servers))


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Имитатор реле шлагбаума</title>
<style>body{font:15px system-ui,sans-serif;margin:24px;background:#f4f6fa;color:#111}h1{font-size:20px}
.card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 1px 3px #0002;margin-bottom:14px;max-width:640px}
.state{font-size:26px;font-weight:800}.coil{display:inline-block;width:26px;height:26px;border-radius:6px;margin:2px;
background:#e5e7eb;text-align:center;line-height:26px;font:600 12px monospace}.on{background:#16a34a;color:#fff}
table{border-collapse:collapse;width:100%;font:13px monospace}td{padding:3px 6px;border-bottom:1px solid #eee}
button{margin-right:6px;padding:6px 10px;border-radius:8px;border:1px solid #ccc;background:#fff;cursor:pointer}</style>
</head><body><h1>Имитатор Modbus-реле со шлагбаумом</h1>
<div class="card"><svg viewBox="0 -200 300 320" width="300" height="320"><rect x="20" y="40" width="40" height="80" fill="#f97316"/>
<g id="boom" style="transform-origin:40px 50px;transition:transform 1.5s"><rect x="40" y="44" width="240" height="12" rx="6" fill="#e11d48"/>
<rect x="80" y="44" width="30" height="12" fill="#fff"/><rect x="150" y="44" width="30" height="12" fill="#fff"/>
<rect x="220" y="44" width="30" height="12" fill="#fff"/></g></svg>
<div class="state" id="st">…</div><div id="meta"></div></div>
<div class="card">Катушки реле: <span id="coils"></span></div>
<div class="card">Режим: <b id="mode"></b><br><br>
<button onclick="mode('ok')">работает</button><button onclick="mode('refuse')">рвёт связь</button>
<button onclick="mode('timeout')">не отвечает</button><button onclick="mode('exception')">ошибка Modbus</button></div>
<div class="card"><b>Журнал</b><table id="log"></table></div>
<script>
const names={CLOSED:"Закрыт",OPENING:"Открывается…",OPEN:"Открыт",CLOSING:"Закрывается…"};
async function tick(){try{const s=await (await fetch('/state')).json();
document.getElementById('st').textContent=names[s.boom]||s.boom;
document.getElementById('boom').style.transform=(s.boom==='OPEN'||s.boom==='OPENING')?'rotate(-80deg)':'rotate(0deg)';
document.getElementById('meta').textContent='открытий: '+s.opens+' · запросов Modbus: '+s.requests+' · автозакрытие через '+s.auto_close_seconds+' с';
document.getElementById('coils').innerHTML=s.coils.map((c,i)=>'<span class="coil '+(c?'on':'')+'">'+i+'</span>').join('');
document.getElementById('mode').textContent=s.mode;
document.getElementById('log').innerHTML=s.journal.map(j=>'<tr><td>'+j.at+'</td><td>'+j.event+'</td><td>'+(j.peer||j.mode||'')+'</td></tr>').join('');
}catch(e){}}
async function mode(m){await fetch('/mode',{method:'POST',body:JSON.stringify({mode:m})});tick();}
setInterval(tick,500);tick();
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Modbus TCP relay + barrier simulator")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — имитатор для локальной сети стенда
    parser.add_argument("--port", type=int, default=1502)
    parser.add_argument("--http-port", type=int, default=8098)
    parser.add_argument("--auto-close", type=float, default=5.0)
    args = parser.parse_args()
    sim = RelaySimulator(auto_close=args.auto_close)
    try:
        asyncio.run(sim.serve(args.host, args.port, args.http_port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
