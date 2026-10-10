"""Драйверы шлагбаумов. Все резолвят DNS до подключения и проверяют адреса политикой SSRF (domain/endpoint.py),
подключаются к проверенному IP (не к имени — иначе DNS rebinding между проверкой и подключением), без редиректов,
таймаут 3 с. HTTP-клиент — собственный, не тот, что ходит к платёжному провайдеру. Fake — только в тестах."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import struct
import time
from uuid import UUID

import defusedxml.ElementTree as DefusedET
import httpx

from parking.modules.devices.application.ports import (
    BarrierDriver,
    BarrierInfo,
    CredentialBox,
    DriverResult,
    EndpointResolver,
    SecretProvider,
)
from parking.modules.devices.domain.endpoint import IPAddress, check_addresses, parse_endpoint
from parking.platform.http.client import make_async_client
from parking.platform.logging import get_logger
from parking.shared.errors import ServiceUnavailableError, ValidationError

log = get_logger("devices.driver")


class DriverRegistry:
    def __init__(self, *, enabled: bool, drivers: dict[str, BarrierDriver] | None = None) -> None:
        self._enabled = enabled
        self._drivers = drivers or {}

    def driver_for(self, barrier: BarrierInfo) -> BarrierDriver:
        if not self._enabled:
            raise ServiceUnavailableError("barrier drivers are disabled (PARKING_BARRIER_DRIVERS_ENABLED=false)")
        driver = self._drivers.get(barrier.driver)
        if driver is None:
            raise ServiceUnavailableError(f"no driver registered for barrier type {barrier.driver}")
        return driver


async def _resolve_checked(resolver: EndpointResolver, host: str, allowlist: tuple[str, ...]) -> IPAddress:
    addresses = await resolver.resolve(host)
    check_addresses(addresses, allowlist)
    return addresses[0]


def _host_literal(ip: IPAddress) -> str:
    return f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)


class HttpRestDriver:
    """POST {endpoint}/open | /close с Bearer-секретом по auth_secret_ref; ответ 2xx — успех."""

    def __init__(self, *, resolver: EndpointResolver, secrets: SecretProvider, timeout: float = 3.0) -> None:
        self._resolver = resolver
        self._secrets = secrets
        self._client = make_async_client(timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def open(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        return await self._call(barrier, command_id, "open")

    async def close(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        return await self._call(barrier, command_id, "close")

    async def probe(self, barrier: BarrierInfo) -> DriverResult:
        """TCP-подключение к проверенному адресу контроллера: стрела не двигается, секрет не отправляется."""
        started = time.perf_counter()
        try:
            parsed = parse_endpoint(barrier.endpoint, driver="HTTP_REST")
            ip = await _resolve_checked(self._resolver, parsed.host, barrier.allowlist)
        except ValidationError as exc:
            return DriverResult(ok=False, error=f"endpoint rejected: {exc.message}")
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(str(ip), parsed.port), timeout=3.0)
        except (OSError, TimeoutError) as exc:
            return DriverResult(ok=False, error=f"connect failed: {type(exc).__name__}", latency_ms=_ms(started))
        writer.close()
        return DriverResult(ok=True, latency_ms=_ms(started), detail=f"tcp {ip}:{parsed.port}")

    async def _call(self, barrier: BarrierInfo, command_id: UUID, action: str) -> DriverResult:
        started = time.perf_counter()
        try:
            parsed = parse_endpoint(barrier.endpoint, driver="HTTP_REST")
            ip = await _resolve_checked(self._resolver, parsed.host, barrier.allowlist)
        except ValidationError as exc:
            log.warning("barrier_endpoint_rejected", barrier_id=str(barrier.id), reason=exc.message)
            return DriverResult(ok=False, error=f"endpoint rejected: {exc.message}")
        if barrier.auth_secret_ref is None:
            return DriverResult(ok=False, error="auth_secret_ref is missing")
        secret = self._secrets.get(barrier.auth_secret_ref)
        path = parsed.path.rstrip("/") + "/" + action
        url = f"{parsed.scheme}://{_host_literal(ip)}:{parsed.port}{path}"
        headers = {"Host": parsed.host, "Authorization": f"Bearer {secret}", "X-Command-Id": str(command_id)}
        extensions = {"sni_hostname": parsed.host} if parsed.scheme == "https" else {}
        try:
            response = await self._client.post(
                url, json={"command_id": str(command_id)}, headers=headers, extensions=extensions
            )
        except httpx.HTTPError as exc:
            return DriverResult(ok=False, error=f"{type(exc).__name__}", latency_ms=_ms(started))
        if response.is_redirect:
            return DriverResult(ok=False, error="redirect refused", latency_ms=_ms(started))
        if response.is_success:
            return DriverResult(ok=True, latency_ms=_ms(started))
        return DriverResult(ok=False, error=f"controller returned {response.status_code}", latency_ms=_ms(started))


class ModbusTcpDriver:
    """Сухой контакт через IP-релей: Modbus TCP FC05 Write Single Coil, импульс `pulse_ms` (ON → OFF).
    Endpoint: host[:port][?unit=1&open_coil=0&close_coil=1&pulse_ms=500]."""

    def __init__(self, *, resolver: EndpointResolver, timeout: float = 3.0, sleep: object = None) -> None:
        self._resolver = resolver
        self._timeout = timeout
        self._sleep = sleep or asyncio.sleep
        self._transaction = 0

    async def open(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        return await self._pulse(barrier, "open_coil", 0)

    async def close(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        return await self._pulse(barrier, "close_coil", 1)

    async def probe(self, barrier: BarrierInfo) -> DriverResult:
        """FC01 Read Coils (8 шт. с адреса 0): реле ответило — связь есть; стрела не двигается."""
        started = time.perf_counter()
        try:
            parsed = parse_endpoint(barrier.endpoint, driver="MODBUS_TCP")
            ip = await _resolve_checked(self._resolver, parsed.host, barrier.allowlist)
        except ValidationError as exc:
            return DriverResult(ok=False, error=f"endpoint rejected: {exc.message}")
        unit = int((parsed.query or {}).get("unit", "1"))
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(str(ip), parsed.port), timeout=self._timeout
            )
        except (OSError, TimeoutError) as exc:
            return DriverResult(ok=False, error=f"connect failed: {type(exc).__name__}", latency_ms=_ms(started))
        try:
            body = await self._request(reader, writer, unit, struct.pack(">BHH", 0x01, 0, 8))
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError) as exc:
            return DriverResult(ok=False, error=f"modbus error: {exc}", latency_ms=_ms(started))
        finally:
            writer.close()
        bits = body[2] if len(body) > 2 else 0
        states = "".join("1" if bits >> i & 1 else "0" for i in range(8))
        return DriverResult(ok=True, latency_ms=_ms(started), detail=f"modbus unit {unit}, coils 0-7: {states}")

    async def _pulse(self, barrier: BarrierInfo, coil_param: str, default_coil: int) -> DriverResult:
        started = time.perf_counter()
        try:
            parsed = parse_endpoint(barrier.endpoint, driver="MODBUS_TCP")
            ip = await _resolve_checked(self._resolver, parsed.host, barrier.allowlist)
        except ValidationError as exc:
            log.warning("barrier_endpoint_rejected", barrier_id=str(barrier.id), reason=exc.message)
            return DriverResult(ok=False, error=f"endpoint rejected: {exc.message}")
        query = parsed.query or {}
        unit = int(query.get("unit", "1"))
        coil = int(query.get(coil_param, str(default_coil)))
        pulse_ms = int(query.get("pulse_ms", "500"))
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(str(ip), parsed.port), timeout=self._timeout
            )
        except (OSError, TimeoutError) as exc:
            return DriverResult(ok=False, error=f"connect failed: {type(exc).__name__}", latency_ms=_ms(started))
        try:
            await self._write_coil(reader, writer, unit, coil, on=True)
            await self._sleep(pulse_ms / 1000)  # type: ignore[operator]
            await self._write_coil(reader, writer, unit, coil, on=False)
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError) as exc:  # реле оборвало связь
            return DriverResult(ok=False, error=f"modbus error: {exc}", latency_ms=_ms(started))
        finally:
            writer.close()
        return DriverResult(ok=True, latency_ms=_ms(started))

    async def _write_coil(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, unit: int, coil: int, *, on: bool
    ) -> None:
        await self._request(reader, writer, unit, struct.pack(">BHH", 0x05, coil, 0xFF00 if on else 0x0000))

    async def _request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, unit: int, pdu: bytes
    ) -> bytes:
        self._transaction = (self._transaction + 1) % 0xFFFF
        frame = struct.pack(">HHHB", self._transaction, 0, len(pdu) + 1, unit) + pdu
        writer.write(frame)
        await asyncio.wait_for(writer.drain(), timeout=self._timeout)
        header = await asyncio.wait_for(reader.readexactly(7), timeout=self._timeout)
        tid, _proto, length, _unit = struct.unpack(">HHHB", header)
        body = await asyncio.wait_for(reader.readexactly(length - 1), timeout=self._timeout)
        if tid != self._transaction:
            raise ValueError("transaction id mismatch")
        if body[0] & 0x80:
            raise ValueError(f"exception code {body[1] if len(body) > 1 else '?'}")
        return body


_ISAPI_NS = "http://www.hikvision.com/ver20/XMLSchema"


class HikvisionIsapiDriver:
    """Реле самой ANPR-камеры Hikvision (DS-TCG406-E и др.) по ISAPI с Digest-авторизацией — без отдельного реле.

    mode=barrier (по умолчанию): PUT /ISAPI/Parking/channels/{channel}/barrierGate, тело
    <BarrierGate><ctrlMode>open|close</ctrlMode></BarrierGate> — камера сама замыкает свой выход управления стрелой.
    mode=io: импульс выхода тревоги PUT /ISAPI/System/IO/outputs/{output}/trigger (high → pulse_ms → low) — для камер
    без раздела Parking. Проверка связи — GET /ISAPI/System/deviceInfo: стрела не двигается, логин проверяется.
    Логин и пароль — в barrier.credentials_encrypted (ключ клиента), расшифровываются только на время запроса."""

    def __init__(
        self, *, resolver: EndpointResolver, box: CredentialBox, timeout: float = 3.0, sleep: object = None
    ) -> None:
        self._resolver = resolver
        self._box = box
        self._sleep = sleep or asyncio.sleep
        self._client = make_async_client(timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def open(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        return await self._command(barrier, "open")

    async def close(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        return await self._command(barrier, "close")

    async def probe(self, barrier: BarrierInfo) -> DriverResult:
        started = time.perf_counter()
        prepared = await self._prepare(barrier)
        if isinstance(prepared, DriverResult):
            return prepared
        base, host, auth, _query = prepared
        try:
            response = await self._request("GET", base + "/ISAPI/System/deviceInfo", host, auth)
        except httpx.HTTPError as exc:
            return DriverResult(ok=False, error=f"camera unreachable: {type(exc).__name__}", latency_ms=_ms(started))
        if response.status_code == 401:
            return DriverResult(ok=False, error="camera rejected login or password", latency_ms=_ms(started))
        if not response.is_success:
            return DriverResult(ok=False, error=f"camera returned {response.status_code}", latency_ms=_ms(started))
        info = _xml_values(response.content)
        detail = (
            " · ".join(v for v in (info.get("model"), info.get("firmwareVersion"), info.get("serialNumber")) if v)
            or "ISAPI OK"
        )
        return DriverResult(ok=True, latency_ms=_ms(started), detail=detail)

    async def _command(self, barrier: BarrierInfo, action: str) -> DriverResult:
        started = time.perf_counter()
        prepared = await self._prepare(barrier)
        if isinstance(prepared, DriverResult):
            return prepared
        base, host, auth, query = prepared
        try:
            if query.get("mode", "barrier") == "io":
                if action == "close":  # у выхода тревоги нет «закрыть»: закрывает контроллер стрелы по таймеру/петле
                    return DriverResult(
                        ok=True, latency_ms=_ms(started), detail="io mode: close is done by the barrier"
                    )
                url = f"{base}/ISAPI/System/IO/outputs/{query.get('output', '1')}/trigger"
                response = await self._request("PUT", url, host, auth, _io_body("high"))
                if response.is_success:
                    try:
                        await self._sleep(int(query.get("pulse_ms", "500")) / 1000)  # type: ignore[operator]
                    finally:
                        # выход — обратно в low и при отмене паузы: иначе он остаётся замкнутым, а стрела — открытой
                        response = await self._request("PUT", url, host, auth, _io_body("low"))
            else:
                url = f"{base}/ISAPI/Parking/channels/{query.get('channel', '1')}/barrierGate"
                response = await self._request("PUT", url, host, auth, _gate_body(action))
        except httpx.HTTPError as exc:
            return DriverResult(ok=False, error=f"camera unreachable: {type(exc).__name__}", latency_ms=_ms(started))
        if response.status_code == 401:
            return DriverResult(ok=False, error="camera rejected login or password", latency_ms=_ms(started))
        if not response.is_success:
            status = _xml_values(response.content)
            reason = status.get("subStatusCode") or status.get("statusString") or str(response.status_code)
            return DriverResult(ok=False, error=f"camera refused: {reason}", latency_ms=_ms(started))
        return DriverResult(ok=True, latency_ms=_ms(started))

    async def _prepare(self, barrier: BarrierInfo) -> DriverResult | tuple[str, str, httpx.DigestAuth, dict[str, str]]:
        try:
            parsed = parse_endpoint(barrier.endpoint, driver="HIKVISION_ISAPI")
            ip = await _resolve_checked(self._resolver, parsed.host, barrier.allowlist)
        except ValidationError as exc:
            log.warning("barrier_endpoint_rejected", barrier_id=str(barrier.id), reason=exc.message)
            return DriverResult(ok=False, error=f"endpoint rejected: {exc.message}")
        if barrier.credentials_encrypted is None or barrier.tenant_id is None:
            return DriverResult(ok=False, error="camera login and password are not set")
        try:
            creds = json.loads(
                self._box.open(barrier.tenant_id, f"barrier:{barrier.id}", barrier.credentials_encrypted)
            )
        except Exception as exc:  # ключ клиента не загружен / данные повреждены — без подробностей в ответе
            log.warning("barrier_credentials_unavailable", barrier_id=str(barrier.id), error=type(exc).__name__)
            return DriverResult(ok=False, error="camera credentials are unavailable")
        base = f"{parsed.scheme}://{_host_literal(ip)}:{parsed.port}"
        return base, parsed.host, httpx.DigestAuth(creds["username"], creds["password"]), parsed.query or {}

    async def _request(
        self, method: str, url: str, host: str, auth: httpx.DigestAuth, body: bytes | None = None
    ) -> httpx.Response:
        headers = {"Host": host}
        if body is not None:
            headers["Content-Type"] = "application/xml"
        extensions = {"sni_hostname": host} if url.startswith("https://") else {}
        return await self._client.request(method, url, content=body, headers=headers, auth=auth, extensions=extensions)


def _gate_body(action: str) -> bytes:
    # ровно как кнопка «Открыть/Закрыть» веб-интерфейса камеры (entranceExit.js прошивки V5.4.0 DS-TCG406-E)
    return f"<?xml version='1.0' encoding='UTF-8'?><BarrierGate><ctrlMode>{action}</ctrlMode></BarrierGate>".encode()


def _io_body(state: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><IOPortData version="2.0" xmlns="{_ISAPI_NS}">'
        f"<outputState>{state}</outputState></IOPortData>"
    ).encode()


def _xml_values(raw: bytes) -> dict[str, str]:
    """Плоские теги ответа ISAPI (DeviceInfo / ResponseStatus); DOCTYPE/ENTITY не разбираются (defusedxml)."""
    if len(raw) > 64 * 1024:
        return {}
    try:
        root = DefusedET.fromstring(raw)
    except Exception:
        return {}
    values: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if element.text and element.text.strip() and tag not in values:
            values[tag] = element.text.strip()[:120]
    return values


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
