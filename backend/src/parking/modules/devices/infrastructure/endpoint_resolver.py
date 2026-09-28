"""DNS-резолв хоста контроллера до подключения (SSRF): результат проверяется политикой домена."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from parking.modules.devices.domain.endpoint import IPAddress
from parking.shared.errors import ValidationError


class SystemEndpointResolver:
    async def resolve(self, host: str) -> list[IPAddress]:
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=3.0)
        except (socket.gaierror, TimeoutError) as exc:
            raise ValidationError("endpoint host does not resolve", details={"host": host}) from exc
        seen: dict[str, IPAddress] = {}
        for _family, _type, _proto, _canon, sockaddr in infos:
            seen.setdefault(str(sockaddr[0]), ipaddress.ip_address(sockaddr[0]))
        return list(seen.values())


class StaticEndpointResolver:
    """Для тестов и dev: фиксированная таблица имён (домен → приватный IP и т. п.)."""

    def __init__(self, table: dict[str, list[str]]) -> None:
        self._table = table

    async def resolve(self, host: str) -> list[IPAddress]:
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        if host not in self._table:
            raise ValidationError("endpoint host does not resolve", details={"host": host})
        return [ipaddress.ip_address(ip) for ip in self._table[host]]
