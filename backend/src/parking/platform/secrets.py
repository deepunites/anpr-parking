"""Секреты по ссылке (`auth_secret_ref` шлагбаума, `secret_ref` мерчант-аккаунта): dev/staging — JSON из
`PARKING_SECRETS_JSON`; prod — Vault (фаза 9), `HardenedSettings` запрещает JSON. Значения никогда не логируются."""

from __future__ import annotations

import json
from typing import Protocol

from parking.shared.errors import ServiceUnavailableError


class SecretProvider(Protocol):
    def get(self, ref: str) -> str: ...


class JsonSecretProvider:
    def __init__(self, raw_json: str) -> None:
        self._values: dict[str, str] = json.loads(raw_json) if raw_json.strip() else {}

    def get(self, ref: str) -> str:
        try:
            return self._values[ref]
        except KeyError as exc:
            raise ServiceUnavailableError(f"secret {ref} is not configured") from exc
