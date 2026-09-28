"""Номер транспортного средства — value object с нормализацией и маскирующим представлением (I-10).

Нормализация выполняется ДО хеширования и является частью домена (фаза 2 §1.4): регистр, разделители,
кириллические омоглифы. Коррекция по позиции (O↔0) намеренно не делается — это работа оператора (I-16).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

_CYRILLIC_TO_LATIN = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")
_SEPARATORS = re.compile(r"[\s\-\._/·]+")


class PlateFormat(enum.StrEnum):
    UZ_STANDARD = "UZ_STANDARD"  # 01A123AA
    UZ_LEGAL = "UZ_LEGAL"  # 01123AAA
    FOREIGN = "FOREIGN"  # любая буквенно-цифровая строка 4–12 символов


_PATTERNS: tuple[tuple[PlateFormat, re.Pattern[str]], ...] = (
    (PlateFormat.UZ_STANDARD, re.compile(r"^\d{2}[A-Z]\d{3}[A-Z]{2}$")),
    (PlateFormat.UZ_LEGAL, re.compile(r"^\d{5}[A-Z]{3}$")),
    (PlateFormat.FOREIGN, re.compile(r"^[A-Z0-9]{4,12}$")),
)


class InvalidPlateError(ValueError):
    """Строка не похожа на номер: после нормализации не подходит ни под один формат."""

    def __init__(self, length: int) -> None:
        # значение намеренно не попадает в сообщение — оно может уйти в лог
        super().__init__(f"invalid plate (normalized length {length})")


def normalize(raw: str) -> str:
    """Регистр → верхний, разделители убраны, кириллические омоглифы → латиница."""
    return _SEPARATORS.sub("", raw.strip().upper()).translate(_CYRILLIC_TO_LATIN)


def mask(value: str) -> str:
    """`01A123AA` → `01A***AA`; короткие строки маскируются почти целиком."""
    if len(value) >= 6:
        return value[:3] + "*" * (len(value) - 5) + value[-2:]
    return value[:1] + "*" * (len(value) - 1)


@dataclass(frozen=True, slots=True)
class PlateNumber:
    value: str
    format: PlateFormat

    @classmethod
    def parse(cls, raw: str) -> PlateNumber:
        """Нормализует и распознаёт формат. Бросает InvalidPlateError."""
        value = normalize(raw)
        for fmt, pattern in _PATTERNS:
            if pattern.match(value):
                return cls(value, fmt)
        raise InvalidPlateError(len(value))

    @property
    def masked(self) -> str:
        return mask(self.value)

    def __str__(self) -> str:
        return self.masked

    def __repr__(self) -> str:
        return f"PlateNumber({self.masked}, {self.format.value})"
