"""Ключи tenant из HashiCorp Vault (prod: PARKING_KEYS_SOURCE=vault). Фаза 10: до этого prod-конфиг требовал Vault,
а провайдер не был реализован — production не стартовал бы.

AppRole login → KV v2 `GET /v1/<mount>/data/<path>` → {"<tenant_id>": {"hmac": b64, "dek": b64, "version": n}} —
формат тот же, что у EnvKeyProvider (PARKING_TENANT_KEYS_JSON). Ключи читаются при старте синхронно (fail-fast,
I-14) и держатся в памяти (A-05: Vault не на горячем пути); `refresh()` перечитывает их (ротация: новая версия в
Vault → вызов из maintenance-задачи или при старте пода). Только httpx: без hvac, без фонового потока.
"""

from __future__ import annotations

import base64
import json
import threading
from uuid import UUID

import httpx

from parking.platform.crypto import TenantKeys
from parking.platform.http.client import make_sync_client
from parking.platform.logging import get_logger

log = get_logger("vault")


class VaultKeyProvider:
    def __init__(
        self,
        *,
        addr: str,
        role_id: str,
        secret_id: str,
        mount: str = "kv",
        path: str = "parking/production/tenant-keys",
        namespace: str | None = None,
        ca_file: str | None = None,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._role_id, self._secret_id = role_id, secret_id
        self._mount, self._path, self._namespace = mount, path, namespace
        self._client = make_sync_client(
            base_url=self._addr, timeout=timeout, verify=ca_file or True, transport=transport
        )
        self._lock = threading.Lock()
        self._keys: dict[UUID, TenantKeys] = {}
        self.refresh()

    def _login(self) -> str:
        headers = {"X-Vault-Namespace": self._namespace} if self._namespace else {}
        resp = self._client.post(
            "/v1/auth/approle/login", json={"role_id": self._role_id, "secret_id": self._secret_id}, headers=headers
        )
        resp.raise_for_status()
        token = str(resp.json()["auth"]["client_token"])
        return token

    def refresh(self) -> int:
        """Перечитывает ключи; при ошибке оставляет прежние (если они есть) и бросает исключение при старте."""
        token = self._login()
        headers = {"X-Vault-Token": token}
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        resp = self._client.get(f"/v1/{self._mount}/data/{self._path}", headers=headers)
        resp.raise_for_status()
        data = resp.json()["data"]["data"]
        raw = data["tenant_keys_json"] if "tenant_keys_json" in data else json.dumps(data)
        parsed: dict[str, dict[str, object]] = json.loads(raw) if isinstance(raw, str) else raw
        keys = {
            UUID(tenant): TenantKeys(
                hmac_key=base64.b64decode(str(entry["hmac"])),
                dek=base64.b64decode(str(entry["dek"])),
                version=int(str(entry.get("version", 1))),
            )
            for tenant, entry in parsed.items()
        }
        with self._lock:
            rotated = sum(1 for t, k in keys.items() if t in self._keys and self._keys[t].version != k.version)
            self._keys = keys
        log.info("vault_keys_loaded", tenants=len(keys), rotated=rotated)
        return len(keys)

    def keys_for(self, tenant_id: UUID) -> TenantKeys:
        with self._lock:
            return self._keys[tenant_id]
