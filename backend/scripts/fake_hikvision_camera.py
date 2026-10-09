"""Имитатор ANPR-камеры Hikvision DS-TCG406-E: ISAPI с Digest-авторизацией и реле шлагбаума внутри камеры.

ISAPI (как у камеры, логин/пароль — --user/--password):
  GET  /ISAPI/System/deviceInfo                         — модель, прошивка, серийный номер (проверка связи)
  PUT  /ISAPI/Parking/channels/1/barrierGate            — <BarrierGate><ctrlMode>open|close|lock|unlock</ctrlMode>
  PUT  /ISAPI/System/IO/outputs/1/trigger               — <IOPortData><outputState>high|low</outputState>
Без авторизации: GET /state — JSON (стрела, число открытий, журнал), POST /mode {"mode": "ok"|"refuse"|"timeout"}.

Отправка события как от настоящей камеры («HTTP-прослушивание»): multipart с частями anpr.xml и
licensePlatePicture.jpg на адрес сервера с ключом камеры в пути:

    python scripts/fake_hikvision_camera.py --port 8099 --user admin --password 'Camera-Pass-1'
    python scripts/fake_hikvision_camera.py push http://127.0.0.1:8010/api/v1/camera/vendor/hikvision/<ключ> 01A777AA

В системе: шлагбаум с драйвером «Камера Hikvision», адрес http://<IP>:8099, логин/пароль как выше, подсеть IP —
в «Подсетях контроллеров стрел» площадки (127.0.0.1 не подойдёт: SSRF-политика запрещает loopback).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

NS = "http://www.hikvision.com/ver20/XMLSchema"
REALM = "DS-TCG406-E"
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")  # минимальный JPEG-маркер


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def _status(code: int, text: str, sub: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><ResponseStatus version="2.0" xmlns="{NS}">'
        f"<requestURL>/ISAPI</requestURL><statusCode>{code}</statusCode><statusString>{text}</statusString>"
        f"<subStatusCode>{sub}</subStatusCode></ResponseStatus>"
    ).encode()


@dataclass
class CameraState:
    boom: str = "CLOSED"
    opens: int = 0
    closes: int = 0
    io_pulses: int = 0
    output: str = "low"
    mode: str = "ok"
    log: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.log.insert(0, f"{datetime.now().strftime('%H:%M:%S')} {text}")
        del self.log[30:]


class HikvisionCameraSimulator:
    def __init__(self, *, user: str = "admin", password: str = "Camera-Pass-1") -> None:  # noqa: S107 — стенд
        self.user = user
        self.password = password
        self.state = CameraState()
        self._nonces: set[str] = set()
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    # ---------------------------------------------------------------- HTTP
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            body = await reader.readexactly(int(headers.get("content-length", "0") or 0))
            status, ctype, payload, extra = await self._route(method, path, headers, body)
            writer.write(
                (
                    f"HTTP/1.1 {status} {'OK' if status < 400 else 'Error'}\r\nContent-Type: {ctype}\r\n"
                    f"Content-Length: {len(payload)}\r\n{extra}Connection: close\r\n\r\n"
                ).encode()
                + payload
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _route(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, str, bytes, str]:
        if path == "/state":
            data = {
                "boom": self.state.boom,
                "opens": self.state.opens,
                "closes": self.state.closes,
                "io_pulses": self.state.io_pulses,
                "output": self.state.output,
                "mode": self.state.mode,
                "log": self.state.log,
            }
            return 200, "application/json", json.dumps(data, ensure_ascii=False).encode(), ""
        if path == "/mode" and method == "POST":
            self.state.mode = json.loads(body or b"{}").get("mode", "ok")
            return 200, "application/json", b'{"ok": true}', ""
        if not path.startswith("/ISAPI/"):
            return 404, "text/plain", b"not found", ""
        if not self._authorized(method, path, headers.get("authorization", "")):
            nonce = secrets.token_hex(16)
            self._nonces.add(nonce)
            challenge = f'WWW-Authenticate: Digest realm="{REALM}", qop="auth", nonce="{nonce}", algorithm=MD5\r\n'
            return 401, "application/xml", _status(4, "Invalid Operation", "notAuthenticated"), challenge
        if self.state.mode == "timeout":
            await asyncio.sleep(30)
        if self.state.mode == "refuse":
            return 403, "application/xml", _status(4, "Invalid Operation", "notSupport"), ""
        return self._isapi(method, path, body)

    def _isapi(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes, str]:
        xml = "application/xml"
        if method == "GET" and path == "/ISAPI/System/deviceInfo":
            info = (
                f'<?xml version="1.0" encoding="UTF-8"?><DeviceInfo version="2.0" xmlns="{NS}">'
                "<deviceName>Entrance ANPR</deviceName><model>DS-TCG406-E</model>"
                "<serialNumber>DS-TCG406-E20260901AAWRSIM0001</serialNumber>"
                "<firmwareVersion>V5.6.12</firmwareVersion><deviceType>IPCamera</deviceType></DeviceInfo>"
            )
            return 200, xml, info.encode(), ""
        gate = re.fullmatch(r"/ISAPI/Parking/channels/(\d+)/barrierGate", path)
        if method == "PUT" and gate:
            match = re.search(rb"<ctrlMode>\s*(\w+)\s*</ctrlMode>", body)
            mode = match.group(1).decode() if match else ""
            if mode not in {"open", "close", "lock", "unlock"}:
                return 400, xml, _status(5, "Invalid XML Content", "badXmlContent"), ""
            if mode == "open":
                self.state.boom, self.state.opens = "OPEN", self.state.opens + 1
            elif mode == "close":
                self.state.boom, self.state.closes = "CLOSED", self.state.closes + 1
            self.state.note(f"barrierGate {mode} (channel {gate.group(1)})")
            return 200, xml, _status(1, "OK", "ok"), ""
        io = re.fullmatch(r"/ISAPI/System/IO/outputs/(\d+)/trigger", path)
        if method == "PUT" and io:
            match = re.search(rb"<outputState>\s*(\w+)\s*</outputState>", body)
            state = match.group(1).decode() if match else ""
            if state not in {"high", "low"}:
                return 400, xml, _status(5, "Invalid XML Content", "badXmlContent"), ""
            if state == "high" and self.state.output == "low":
                self.state.io_pulses += 1
            self.state.output = state
            self.state.note(f"alarm output {io.group(1)} {state}")
            return 200, xml, _status(1, "OK", "ok"), ""
        return 404, xml, _status(4, "Invalid Operation", "notSupport"), ""

    def _authorized(self, method: str, path: str, header: str) -> bool:
        if not header.lower().startswith("digest "):
            return False
        parts = dict(re.findall(r'(\w+)="?([^",]+)"?', header[7:]))
        if parts.get("username") != self.user or parts.get("nonce") not in self._nonces:
            return False
        ha1 = _md5(f"{self.user}:{REALM}:{self.password}")
        ha2 = _md5(f"{method}:{parts.get('uri', path)}")
        expected = _md5(f"{ha1}:{parts['nonce']}:{parts.get('nc')}:{parts.get('cnonce')}:{parts.get('qop')}:{ha2}")
        return secrets.compare_digest(expected, parts.get("response", ""))


# ---------------------------------------------------------------- отправка события как от камеры
def anpr_xml(plate: str, *, confidence: int = 96, when: datetime | None = None) -> bytes:
    moment = (when or datetime.now(tz=UTC)).isoformat()
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><EventNotificationAlert version="2.0" xmlns="{NS}">'
        f"<ipAddress>192.168.1.64</ipAddress><portNo>80</portNo><protocol>HTTP</protocol><channelID>1</channelID>"
        f"<dateTime>{moment}</dateTime><activePostCount>1</activePostCount><eventType>ANPR</eventType>"
        f"<eventState>active</eventState><eventDescription>ANPR</eventDescription><ANPR><country>UZ</country>"
        f"<licensePlate>{plate}</licensePlate><line>1</line><direction>forward</direction>"
        f"<confidenceLevel>{confidence}</confidenceLevel><plateType>unknown</plateType><plateColor>white</plateColor>"
        f"<vehicleType>vehicle</vehicleType></ANPR></EventNotificationAlert>"
    ).encode()


async def push(url: str, plate: str, confidence: int = 96) -> tuple[int, str]:
    import httpx

    files = [
        ("anpr.xml", ("anpr.xml", anpr_xml(plate, confidence=confidence), "text/xml")),
        ("licensePlatePicture.jpg", ("licensePlatePicture.jpg", JPEG, "image/jpeg")),
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, files=files)
    return response.status_code, response.text


async def _serve(args: argparse.Namespace) -> None:
    sim = HikvisionCameraSimulator(user=args.user, password=args.password)
    await sim.start(args.host, args.port)
    print(f"Hikvision camera simulator on {args.host}:{sim.port} (user {args.user})", flush=True)
    await asyncio.Event().wait()  # до Ctrl+C


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "push":
        parser = argparse.ArgumentParser(description="send an ANPR event like a Hikvision camera")
        parser.add_argument("cmd")
        parser.add_argument("url")
        parser.add_argument("plate")
        parser.add_argument("--confidence", type=int, default=96)
        args = parser.parse_args()
        started = time.perf_counter()
        status, text = asyncio.run(push(args.url, args.plate, args.confidence))
        print(f"HTTP {status} за {(time.perf_counter() - started) * 1000:.0f} мс: {text}")
        return
    parser = argparse.ArgumentParser(description="Hikvision DS-TCG406-E ISAPI simulator")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — имитатор для локальной сети стенда
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="Camera-Pass-1")
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()
