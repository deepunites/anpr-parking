"""Логины сотрудника: e-mail и телефон. Телефон — в E.164: один сотрудник не заводится дважды под разной записью."""

from __future__ import annotations

import re

from parking.shared.errors import ValidationError

_E164 = re.compile(r"^\+[0-9]{9,15}$")
_SEPARATORS = re.compile(r"[\s\-().]")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
UZ_PREFIX = "998"


def normalize_phone(raw: str) -> str:
    """«+998 90 123-45-67», «998901234567», «90 123 45 67» → «+998901234567». Иначе INVALID_PHONE."""
    value = _SEPARATORS.sub("", raw.strip())
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value.startswith("+"):
        if len(value) == 9 and value.isdigit():  # местный номер без кода страны
            value = UZ_PREFIX + value
        value = "+" + value
    if not _E164.match(value):
        raise ValidationError("invalid phone", code="INVALID_PHONE")
    return value


def normalize_email(raw: str) -> str:
    value = raw.strip().lower()
    if not _EMAIL.match(value) or len(value) > 254:
        raise ValidationError("invalid email", code="INVALID_EMAIL")
    return value


def normalize_login(raw: str) -> str | None:
    """Логин на входе: e-mail или телефон. None — не похоже ни на то, ни на другое (ответ как «неверные данные»)."""
    try:
        return normalize_email(raw) if "@" in raw else normalize_phone(raw)
    except ValidationError:
        return None
