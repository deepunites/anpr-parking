"""Деньги — целые в минорных единицах на всём пути (I-13).

`Money` не приводится к float/int, складывается только в одной валюте, умножается на `Fraction`/`Decimal`
с явным режимом округления. Форматирование — здесь, чтобы фронт не занимался арифметикой.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Self


class Currency(enum.StrEnum):
    UZS = "UZS"


MINOR_DIGITS: dict[Currency, int] = {Currency.UZS: 2}
CURRENCY_LABEL: dict[Currency, dict[str, str]] = {Currency.UZS: {"ru": "сум", "uz": "so‘m"}}


class Rounding(enum.StrEnum):
    """Режим округления результата умножения/деления к целым минорным единицам."""

    UP = "UP"  # к +∞ (в пользу парковки)
    DOWN = "DOWN"  # к −∞ (в пользу клиента)
    HALF_UP = "HALF_UP"  # 0.5 → вверх
    HALF_EVEN = "HALF_EVEN"  # банковское


class MoneyError(ValueError):
    """Неверная операция с деньгами (разные валюты, float, отрицательный делитель)."""


def _round(value: Fraction, mode: Rounding) -> int:
    if mode is Rounding.UP:
        return math.ceil(value)
    if mode is Rounding.DOWN:
        return math.floor(value)
    floor = math.floor(value)
    remainder = value - floor
    if remainder > Fraction(1, 2):
        return floor + 1
    if remainder < Fraction(1, 2):
        return floor
    if mode is Rounding.HALF_UP:
        return floor + 1
    return floor if floor % 2 == 0 else floor + 1


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: Currency = Currency.UZS

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise MoneyError(f"minor must be int, got {type(self.minor).__name__}")

    # --- арифметика -------------------------------------------------------
    def _check(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise MoneyError(f"expected Money, got {type(other).__name__}")
        if other.currency is not self.currency:
            raise MoneyError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __mul__(self, factor: int) -> Money:
        """Умножение только на целое — точное. Для долей — `multiply()` с режимом округления."""
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise MoneyError("use Money.multiply(Fraction | Decimal, rounding) for non-integer factors")
        return Money(self.minor * factor, self.currency)

    __rmul__ = __mul__

    def multiply(self, factor: Fraction | Decimal, rounding: Rounding) -> Money:
        """Умножает на точную дробь с явным округлением. Бросает MoneyError на float."""
        if isinstance(factor, float):  # type: ignore[unreachable]  # защита от вызова без проверки типов
            raise MoneyError("float is forbidden in money arithmetic")
        if isinstance(factor, Decimal):
            if not factor.is_finite():
                raise MoneyError("non-finite Decimal")
            factor = Fraction(factor)
        elif not isinstance(factor, Fraction):
            raise MoneyError(f"factor must be Fraction or Decimal, got {type(factor).__name__}")
        return Money(_round(Fraction(self.minor) * factor, rounding), self.currency)

    def percent(self, percent: int, rounding: Rounding) -> Money:
        """`percent` % от суммы (например, размер скидки). Сумма и остаток вместе всегда равны исходной."""
        if not 0 <= percent <= 100:
            raise MoneyError("percent must be within 0..100")
        return self.multiply(Fraction(percent, 100), rounding)

    def split(self, parts: int) -> list[Money]:
        """Делит на `parts` частей, различающихся не более чем на 1 минорную единицу; сумма частей == исходная."""
        if parts <= 0:
            raise MoneyError("parts must be positive")
        base, remainder = divmod(self.minor, parts)
        return [Money(base + (1 if i < remainder else 0), self.currency) for i in range(parts)]

    # --- сравнение -------------------------------------------------------
    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.minor >= other.minor

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    @property
    def is_negative(self) -> bool:
        return self.minor < 0

    @classmethod
    def zero(cls, currency: Currency = Currency.UZS) -> Self:
        return cls(0, currency)

    @classmethod
    def from_fraction(cls, value: Fraction, rounding: Rounding, currency: Currency = Currency.UZS) -> Self:
        """Точная дробь минорных единиц → Money с явным округлением."""
        return cls(_round(value, rounding), currency)

    # --- представление ---------------------------------------------------
    def formatted(self, locale: str = "ru") -> str:
        """«15 000,00 сум»: разряды через пробел, десятичная запятая, метка валюты по локали."""
        digits = MINOR_DIGITS[self.currency]
        sign = "-" if self.minor < 0 else ""
        major, minor = divmod(abs(self.minor), 10**digits)
        groups = f"{major:,}".replace(",", " ")
        label = CURRENCY_LABEL[self.currency].get(locale, CURRENCY_LABEL[self.currency]["ru"])
        return f"{sign}{groups},{minor:0{digits}d} {label}"

    def to_dict(self, locale: str = "ru") -> dict[str, int | str]:
        return {"minor": self.minor, "currency": self.currency.value, "formatted": self.formatted(locale)}

    def __repr__(self) -> str:
        return f"Money({self.minor}, {self.currency.value})"
