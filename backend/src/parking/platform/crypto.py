"""Криптография номеров (фаза 2 §1.4): HMAC-SHA256 для поиска, AES-256-GCM для хранения, ключи per-tenant в памяти.

Ключевой материал приходит из `KeyProvider` (Vault в prod, окружение в dev — выбирается конфигом, фаза 3.1).
Vault на горячем пути отсутствует (A-05): `KeyRing` держит ключи процесса и обновляется при ротации.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from parking.shared.plate import PlateNumber

NONCE_LEN = 12
TAG_LEN = 16


@dataclass(frozen=True, slots=True)
class TenantKeys:
    hmac_key: bytes
    dek: bytes
    version: int

    def __post_init__(self) -> None:
        if len(self.hmac_key) != 32 or len(self.dek) != 32:
            raise ValueError("tenant keys must be 32 bytes each")
        if self.version < 1:
            raise ValueError("key version must be >= 1")


class KeyProvider(Protocol):
    def keys_for(self, tenant_id: UUID) -> TenantKeys:
        """Ключи tenant (текущая версия). Бросает KeyError, если tenant неизвестен."""
        ...


class EnvKeyProvider:
    """Ключи из JSON в переменной окружения. Только dev/staging — prod-конфиг запрещает (config.HardenedSettings)."""

    def __init__(self, raw_json: str) -> None:
        parsed: dict[str, dict[str, object]] = json.loads(raw_json) if raw_json.strip() else {}
        self._keys: dict[UUID, TenantKeys] = {
            UUID(tenant): TenantKeys(
                hmac_key=base64.b64decode(str(entry["hmac"])),
                dek=base64.b64decode(str(entry["dek"])),
                version=int(str(entry.get("version", 1))),
            )
            for tenant, entry in parsed.items()
        }

    def keys_for(self, tenant_id: UUID) -> TenantKeys:
        return self._keys[tenant_id]

    @staticmethod
    def generate_entry(version: int = 1) -> dict[str, object]:
        return {
            "hmac": base64.b64encode(os.urandom(32)).decode(),
            "dek": base64.b64encode(os.urandom(32)).decode(),
            "version": version,
        }


class KeyWrapper:
    """Envelope encryption (v2 этап 3): DEK/HMAC клиента, созданные при регистрации, хранятся в БД обёрнутыми
    ключом платформы (KEK). AES-256-GCM, AAD = tenant:version:purpose — обёртку нельзя переставить между
    клиентами или между HMAC и DEK. KEK живёт только в памяти процесса (Vault/окружение), в БД его нет."""

    WRAPPED_LEN = NONCE_LEN + 32 + TAG_LEN

    def __init__(self, kek: bytes, kek_id: str = "kek-v1") -> None:
        if len(kek) != 32:
            raise ValueError("key wrapping key must be 32 bytes")
        self._aead = AESGCM(kek)
        self.kek_id = kek_id

    @staticmethod
    def _aad(tenant_id: UUID, version: int, purpose: str) -> bytes:
        return f"{tenant_id}:{version}:{purpose}".encode()

    def wrap(self, tenant_id: UUID, keys: TenantKeys) -> tuple[bytes, bytes]:
        out = []
        for purpose, material in (("hmac", keys.hmac_key), ("dek", keys.dek)):
            nonce = os.urandom(NONCE_LEN)
            out.append(nonce + self._aead.encrypt(nonce, material, self._aad(tenant_id, keys.version, purpose)))
        return out[0], out[1]

    def unwrap(self, tenant_id: UUID, version: int, wrapped_hmac: bytes, wrapped_dek: bytes) -> TenantKeys:
        parts = []
        for purpose, blob in (("hmac", wrapped_hmac), ("dek", wrapped_dek)):
            parts.append(self._aead.decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], self._aad(tenant_id, version, purpose)))
        return TenantKeys(hmac_key=parts[0], dek=parts[1], version=version)

    @staticmethod
    def generate(version: int = 1) -> TenantKeys:
        return TenantKeys(hmac_key=os.urandom(32), dek=os.urandom(32), version=version)


class LayeredKeyProvider:
    """Сначала статические ключи (окружение/Vault — клиенты, заведённые до самостоятельной регистрации), затем
    ключи из БД (обёрнутые KEK). Кеш ключей из БД пополняется: сразу после регистрации в том же процессе (`add`)
    и периодическим `refresh` во всех процессах (API, воркеры) — новый клиент появляется без перезапуска.
    Горячий путь синхронный и без I/O (A-05)."""

    def __init__(self, static: KeyProvider) -> None:
        self._static = static
        self._wrapped: dict[UUID, TenantKeys] = {}
        self.on_miss: Callable[[UUID], None] | None = None

    def keys_for(self, tenant_id: UUID) -> TenantKeys:
        try:
            return self._static.keys_for(tenant_id)
        except KeyError:
            pass
        keys = self._wrapped.get(tenant_id)
        if keys is None:
            if self.on_miss is not None:
                self.on_miss(tenant_id)  # запланировать внеочередное обновление кеша
            raise KeyError(tenant_id)
        return keys

    def add(self, tenant_id: UUID, keys: TenantKeys) -> None:
        current = self._wrapped.get(tenant_id)
        if current is None or keys.version >= current.version:
            self._wrapped[tenant_id] = keys

    def known_wrapped(self) -> int:
        return len(self._wrapped)


class PlateCrypto:
    """Хеш и шифр номера для конкретного tenant. Открытый номер не логируется — принимает только PlateNumber."""

    def __init__(self, provider: KeyProvider) -> None:
        self._provider = provider

    def hash(self, tenant_id: UUID, plate: PlateNumber) -> bytes:
        keys = self._provider.keys_for(tenant_id)
        return hmac.new(keys.hmac_key, plate.value.encode("utf-8"), hashlib.sha256).digest()

    def hashes_for_lookup(self, tenant_id: UUID, plate: PlateNumber) -> list[bytes]:
        """В переходный период ротации — хеши всех действующих версий (пока провайдер отдаёт одну)."""
        return [self.hash(tenant_id, plate)]

    def encrypt(self, tenant_id: UUID, plate: PlateNumber) -> tuple[bytes, int]:
        """nonce(12) || ciphertext || tag(16); AAD = tenant_id || version — шифртекст привязан к tenant."""
        keys = self._provider.keys_for(tenant_id)
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(keys.dek).encrypt(nonce, plate.value.encode("utf-8"), _aad(tenant_id, keys.version))
        return nonce + ct, keys.version

    def encrypt_text(self, tenant_id: UUID, text: str) -> bytes:
        """Произвольная строка ПДн субъекта (контакт для ответа) тем же DEK; формат как у номера."""
        keys = self._provider.keys_for(tenant_id)
        nonce = os.urandom(NONCE_LEN)
        return nonce + AESGCM(keys.dek).encrypt(nonce, text.encode("utf-8"), _aad(tenant_id, keys.version))

    def decrypt(self, tenant_id: UUID, blob: bytes, version: int) -> PlateNumber:
        keys = self._provider.keys_for(tenant_id)
        if keys.version != version:
            raise ValueError(f"key version {version} is not loaded for tenant")
        plain = AESGCM(keys.dek).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], _aad(tenant_id, version))
        return PlateNumber.parse(plain.decode("utf-8"))


def _aad(tenant_id: UUID, version: int) -> bytes:
    return tenant_id.bytes + version.to_bytes(2, "big")


def hmac_camera_key(pepper: bytes, key: str) -> bytes:
    """HMAC-SHA256(pepper, ключ камеры) — фаза 4; здесь, чтобы seed и тесты использовали одну функцию."""
    return hmac.new(pepper, key.encode("utf-8"), hashlib.sha256).digest()
