"""Отправка вебхука: адрес резолвится и проверяется SSRF-политикой, подключение — к проверенному IP с Host и SNI
исходного имени (как драйвер стрел): смена DNS между проверкой и запросом ничего не даёт. Редиректы не следуем."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import httpx

from parking.modules.integrations.application.ports import SendResult
from parking.modules.integrations.domain.webhook import check_address
from parking.platform.http.client import make_async_client
from parking.shared.errors import ValidationError


class HttpWebhookSender:
    def __init__(self, *, allow_private: bool, timeout: float = 5.0) -> None:
        self._allow_private = allow_private
        self._client = make_async_client(timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _resolve(self, host: str, port: int) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
        else:
            addresses = [literal]
        if not addresses:
            raise ValidationError("webhook host does not resolve", code="WEBHOOK_ADDRESS_BLOCKED")
        for ip in addresses:  # один «плохой» адрес — отказ целиком
            check_address(ip, allow_private=self._allow_private)
        return addresses[0]

    async def send(self, url: str, body: bytes, headers: dict[str, str]) -> SendResult:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        started = time.perf_counter()
        try:
            ip = await self._resolve(host, port)
        except OSError as exc:
            return SendResult(ok=False, status=None, latency_ms=_ms(started), error=f"dns: {type(exc).__name__}")
        literal = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        target = f"{parts.scheme}://{literal}:{port}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        try:
            response = await self._client.post(
                target, content=body, headers={**headers, "Host": parts.netloc}, extensions=extensions
            )
        except httpx.HTTPError as exc:
            return SendResult(
                ok=False, status=None, latency_ms=_ms(started), error=f"{type(exc).__name__}: {exc}"[:200]
            )
        ok = 200 <= response.status_code < 300
        error = None if ok else f"HTTP {response.status_code}: {response.text[:150]}"
        return SendResult(ok=ok, status=response.status_code, latency_ms=_ms(started), error=error)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
