"""Драйверы шлагбаумов. Оба резолвят DNS до подключения и проверяют адреса политикой SSRF (domain/endpoint.py),
подключаются к проверенному IP (не к имени — иначе DNS rebinding между проверкой и подключением), без редиректов,
таймаут 3 с. HTTP-клиент — собственный, не тот, что ходит к платёжному провайдеру. Fake — только в тестах."""

from __future__ import annotations

import asyncio
import ipaddress
import struct
import time
from uuid import UUID

import httpx

from parking.modules.devices.application.ports import (
    BarrierDriver,
    BarrierInfo,
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


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
