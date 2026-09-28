"""Совместимость: провайдер секретов живёт в platform.secrets (общий для шлагбаумов и платёжных мерчантов)."""

from __future__ import annotations

from parking.platform.secrets import JsonSecretProvider, SecretProvider

__all__ = ["JsonSecretProvider", "SecretProvider"]
