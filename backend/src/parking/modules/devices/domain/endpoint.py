"""SSRF-политика адреса контроллера: разбор endpoint и проверка резолвленных IP без сетевого IO.

Резолв DNS — инфраструктура (`infrastructure/endpoint_resolver.py`); здесь только решение по уже известным адресам:
- блок-лист всегда: loopback, link-local (169.254/16 — метаданные облака), multicast, unspecified, reserved,
  ::1, fd00::/8;
- приватные подсети (10/8, 172.16/12, 192.168/16, fc00::/7) — только внутри `parking_lot.barrier_cidr_allowlist`;
- публичные адреса — разрешены (контроллер за NAT/VPN с публичным адресом — легитимный случай).
Аллоклист задаётся на площадке, не на tenant (фаза 4 §3): у разных площадок разные локальные сети.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from parking.shared.errors import ValidationError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_ALWAYS_BLOCKED: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT — метаданные некоторых облаков (например, 100.100.100.200)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("fd00::/8"),
)
_PRIVATE: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


@dataclass(frozen=True, slots=True)
class ParsedEndpoint:
    scheme: str  # http | https | modbus
    host: str
    port: int
    path: str = "/"
    query: dict[str, str] | None = None

    @property
    def is_ip_literal(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True


HIKVISION_PARAMS = {"channel", "mode", "output", "pulse_ms"}


def _parse_hikvision(value: str) -> ParsedEndpoint:
    """Адрес камеры: http(s)://host[:port][?channel=1&mode=barrier|io&output=1&pulse_ms=500].
    mode=barrier (по умолчанию) — ISAPI /Parking/channels/{channel}/barrierGate (камеры въезда DS-TCG4xx);
    mode=io — импульс выхода тревоги /System/IO/outputs/{output}/trigger (обычные камеры Hikvision)."""
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if parts.scheme == "https":
        # у камер самоподписанный сертификат — проверка TLS всегда падала бы как «camera unreachable»
        raise ValidationError("use http://camera-address: Hikvision cameras have self-signed certificates")
    if parts.scheme != "http" or not parts.hostname:
        raise ValidationError("Hikvision endpoint must be http://camera-address")
    if parts.username or parts.password:
        raise ValidationError("credentials in endpoint URL are not allowed — use login and password fields")
    if parts.path not in {"", "/"}:
        raise ValidationError("Hikvision endpoint must not contain a path")
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    for key, val in query.items():
        if key not in HIKVISION_PARAMS:
            raise ValidationError(f"unsupported Hikvision endpoint parameter {key}")
        if key == "mode" and val not in {"barrier", "io"}:
            raise ValidationError("Hikvision mode must be barrier or io")
        if key != "mode" and not (_digits(val) and 0 < int(val) <= (5000 if key == "pulse_ms" else 16)):
            raise ValidationError(f"invalid Hikvision endpoint parameter {key}")
    return ParsedEndpoint(parts.scheme, parts.hostname, parts.port or 80, "/", query)


def _digits(value: str) -> bool:
    return value.isascii() and value.isdigit()  # «²».isdigit() — True, но int() падает


def parse_endpoint(endpoint: str, *, driver: str) -> ParsedEndpoint:
    """HTTP_REST: URL http(s)://host[:port]/path. MODBUS_TCP: host[:port][?unit=&open_coil=&close_coil=&pulse_ms=].
    HIKVISION_ISAPI: http://camera[:port][?channel=&mode=&output=&pulse_ms=]."""
    try:
        return _parse_endpoint(endpoint, driver=driver)
    except ValueError as exc:  # urlsplit/port: «[fe80::1», «:99999» — ошибка ввода (422), не сбой (500)
        raise ValidationError(f"invalid endpoint: {exc}") from None


def _parse_endpoint(endpoint: str, *, driver: str) -> ParsedEndpoint:
    value = endpoint.strip()
    if driver == "HTTP_REST":
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValidationError("HTTP_REST endpoint must be an http(s) URL with a host")
        if parts.username or parts.password:
            raise ValidationError("credentials in endpoint URL are not allowed")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return ParsedEndpoint(parts.scheme, parts.hostname, port, parts.path or "/")
    if driver == "HIKVISION_ISAPI":
        return _parse_hikvision(value)
    parts = urlsplit("modbus://" + value)
    if not parts.hostname or parts.path not in {"", "/"}:
        raise ValidationError("MODBUS_TCP endpoint must be host[:port][?unit=&open_coil=&close_coil=]")
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    for key, value_ in query.items():
        if key not in {"unit", "open_coil", "close_coil", "pulse_ms"} or not _digits(value_):
            raise ValidationError(f"unsupported MODBUS endpoint parameter {key}")
    return ParsedEndpoint("modbus", parts.hostname, parts.port or 502, "/", query)


def _in_any(ip: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    return any(ip.version == net.version and ip in net for net in networks)


def check_addresses(addresses: list[IPAddress], allowlist: tuple[str, ...]) -> None:
    """Все адреса, в которые резолвится хост, обязаны пройти: один «плохой» A-record — отказ целиком."""
    if not addresses:
        raise ValidationError("endpoint host does not resolve")
    allowed = tuple(ipaddress.ip_network(cidr, strict=False) for cidr in allowlist)
    for ip in addresses:
        target = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if _in_any(target, _ALWAYS_BLOCKED):
            raise ValidationError("endpoint resolves to a blocked address", details={"address": str(target)})
        if _in_any(target, _PRIVATE) and not _in_any(target, allowed):
            raise ValidationError(
                "endpoint resolves to a private address outside lot allowlist", details={"address": str(target)}
            )
