"""Секреты интеграций (подпись вебхуков) — AES-256-GCM ключом клиента (DEK): дамп БД без ключей их не раскрывает.
Формат: version(2) || nonce(12) || ciphertext || tag(16); AAD = tenant || version || назначение."""

from __future__ import annotations

import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from parking.platform.crypto import KeyProvider

NONCE = 12


class TenantSecretBox:
    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys

    @staticmethod
    def _aad(tenant_id: UUID, version: int, purpose: str) -> bytes:
        return tenant_id.bytes + version.to_bytes(2, "big") + purpose.encode()

    def seal(self, tenant_id: UUID, purpose: str, secret: bytes) -> bytes:
        keys = self._keys.keys_for(tenant_id)
        nonce = os.urandom(NONCE)
        sealed = AESGCM(keys.dek).encrypt(nonce, secret, self._aad(tenant_id, keys.version, purpose))
        return keys.version.to_bytes(2, "big") + nonce + sealed

    def open(self, tenant_id: UUID, purpose: str, blob: bytes) -> bytes:
        version = int.from_bytes(blob[:2], "big")
        keys = self._keys.keys_for(tenant_id)
        if keys.version != version:
            raise ValueError(f"key version {version} is not loaded for tenant")
        return AESGCM(keys.dek).decrypt(blob[2 : 2 + NONCE], blob[2 + NONCE :], self._aad(tenant_id, version, purpose))
