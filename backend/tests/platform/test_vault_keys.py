"""Фаза 10: VaultKeyProvider — AppRole login + KV v2 (mock-транспорт httpx): формат ключей как у EnvKeyProvider,
fail-fast при недоступном Vault, refresh() подхватывает ротацию версии без перезапуска."""

from __future__ import annotations

import base64
import json
import os
from uuid import UUID

import httpx
import pytest

from parking.platform.http.vault_keys import VaultKeyProvider

TENANT = UUID("00000000-0000-4000-8000-0000000000a1")


def _entry(version: int) -> dict[str, object]:
    return {
        "hmac": base64.b64encode(os.urandom(32)).decode(),
        "dek": base64.b64encode(os.urandom(32)).decode(),
        "version": version,
    }


class FakeVault:
    def __init__(self) -> None:
        self.version = 1
        self.logins = 0
        self.reads = 0
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/v1/auth/approle/login":
            body = json.loads(request.content)
            assert body == {"role_id": "role", "secret_id": "secret"}
            self.logins += 1
            return httpx.Response(200, json={"auth": {"client_token": "s.token"}})
        if request.url.path == "/v1/kv/data/parking/production/tenant-keys":
            assert request.headers["X-Vault-Token"] == "s.token"
            self.reads += 1
            data = {"tenant_keys_json": json.dumps({str(TENANT): _entry(self.version)})}
            return httpx.Response(200, json={"data": {"data": data, "metadata": {"version": self.version}}})
        return httpx.Response(404)


def test_keys_loaded_at_start_and_refreshed_on_rotation() -> None:
    vault = FakeVault()
    provider = VaultKeyProvider(
        addr="https://vault.test:8200", role_id="role", secret_id="secret", transport=httpx.MockTransport(vault.handler)
    )
    keys = provider.keys_for(TENANT)
    assert keys.version == 1 and len(keys.hmac_key) == 32 and (vault.logins, vault.reads) == (1, 1)
    vault.version = 2
    assert provider.refresh() == 1
    assert provider.keys_for(TENANT).version == 2, "ротация подхвачена без перезапуска"
    with pytest.raises(KeyError):
        provider.keys_for(UUID("00000000-0000-4000-8000-0000000000b1"))


def test_vault_unavailable_is_fail_fast() -> None:
    vault = FakeVault()
    vault.down = True
    with pytest.raises(httpx.ConnectError):  # I-14: без ключей процесс не стартует
        VaultKeyProvider(
            addr="https://vault.test:8200",
            role_id="role",
            secret_id="secret",
            transport=httpx.MockTransport(vault.handler),
        )
