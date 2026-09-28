"""SSRF-политика endpoint шлагбаума (фаза 4 §3): блок-лист, аллоклист площадки, DNS → приватный IP."""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from parking.modules.devices.application.admin import DeviceAdminService
from parking.modules.devices.domain.barrier import BarrierDriverKind
from parking.modules.devices.domain.endpoint import check_addresses, parse_endpoint
from parking.modules.devices.infrastructure.endpoint_resolver import StaticEndpointResolver
from parking.modules.identity.application.contracts import Role
from parking.runtime.container import Container
from parking.shared.errors import ValidationError
from tests.conftest import LOT_A1
from tests.support.api_fixtures import Principal, bearer


def _ips(*values: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return [ipaddress.ip_address(v) for v in values]


def test_parse_http_and_modbus_endpoints() -> None:
    http = parse_endpoint("https://gate.example.com/api/v2", driver="HTTP_REST")
    assert (http.scheme, http.host, http.port, http.path) == ("https", "gate.example.com", 443, "/api/v2")
    modbus = parse_endpoint("10.10.1.20:502?unit=2&open_coil=3", driver="MODBUS_TCP")
    assert (modbus.host, modbus.port, modbus.query) == ("10.10.1.20", 502, {"unit": "2", "open_coil": "3"})
    with pytest.raises(ValidationError):
        parse_endpoint("ftp://x/", driver="HTTP_REST")
    with pytest.raises(ValidationError):
        parse_endpoint("http://user:pw@host/", driver="HTTP_REST")
    with pytest.raises(ValidationError):
        parse_endpoint("host:502?bogus=1", driver="MODBUS_TCP")


@pytest.mark.parametrize("address", ["169.254.169.254", "127.0.0.1", "::1", "fd00::1", "224.0.0.1", "100.100.100.200"])
def test_blocklist_always_rejected(address: str) -> None:
    with pytest.raises(ValidationError):
        check_addresses(_ips(address), allowlist=("0.0.0.0/0", "::/0"))


def test_private_only_within_lot_allowlist() -> None:
    with pytest.raises(ValidationError):
        check_addresses(_ips("10.10.2.5"), allowlist=("10.10.1.0/24",))
    check_addresses(_ips("10.10.1.20"), allowlist=("10.10.1.0/24",))
    check_addresses(_ips("203.0.113.10"), allowlist=())  # публичный адрес — разрешён
    with pytest.raises(ValidationError):  # один плохой адрес среди нескольких — отказ целиком
        check_addresses(_ips("203.0.113.10", "127.0.0.1"), allowlist=())


async def test_dns_resolving_to_private_ip_rejected(container: Container) -> None:
    service = DeviceAdminService(
        uow_factory=container.uow,
        repos=container.devices._repos,
        resolver=StaticEndpointResolver({"gate.internal": ["192.168.1.10"], "meta.evil": ["169.254.169.254"]}),
        auth_cache=container.devices._auth_cache,
        pepper=b"\0" * 32,
        clock=container.clock,
    )
    with pytest.raises(ValidationError, match="private"):
        await service.validate_endpoint("http://gate.internal/open", BarrierDriverKind.HTTP_REST, ())
    with pytest.raises(ValidationError, match="blocked"):
        await service.validate_endpoint("http://meta.evil/", BarrierDriverKind.HTTP_REST, ("192.168.0.0/16",))
    await service.validate_endpoint("http://gate.internal/open", BarrierDriverKind.HTTP_REST, ("192.168.1.0/24",))


async def test_api_rejects_metadata_endpoint(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    body = {
        "lot_id": str(LOT_A1),
        "name": "evil",
        "driver": "HTTP_REST",
        "endpoint": "http://169.254.169.254/latest/meta-data/",
        "auth_secret_ref": "kv/x",
        "reason": "ssrf test",
    }
    response = await client.post("/api/v1/barriers", json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["address"] == "169.254.169.254"
    body["endpoint"] = "http://10.10.9.9:8080/"  # приватный вне аллоклиста площадки (10.10.1.0/24)
    assert (await client.post("/api/v1/barriers", json=body, headers=headers)).status_code == 422
    body["endpoint"] = "http://10.10.1.77:8080/"
    ok = await client.post("/api/v1/barriers", json=body, headers=headers)
    assert ok.status_code == 201, ok.text
