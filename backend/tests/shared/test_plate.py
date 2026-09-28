"""PlateNumber: нормализация узбекских номеров и маскирование (I-10)."""

from __future__ import annotations

import pytest

from parking.shared.plate import InvalidPlateError, PlateFormat, PlateNumber, mask, normalize


@pytest.mark.parametrize(
    "raw", ["01 A 123 AA", "01a123aa", "01А123АА", "01-A-123-AA", " 01A123AA ", "01.a.123.aa", "01а123аа"]
)
def test_same_plate_normalizes_identically(raw: str) -> None:
    plate = PlateNumber.parse(raw)
    assert plate.value == "01A123AA"
    assert plate.format is PlateFormat.UZ_STANDARD


def test_all_cyrillic_homoglyphs_map_to_latin() -> None:
    assert normalize("АВЕКМНОРСТУХ") == "ABEKMHOPCTYX"
    assert normalize("авекмнорстух") == "ABEKMHOPCTYX"


def test_letter_o_is_not_silently_corrected_to_zero() -> None:
    assert PlateNumber.parse("O1A123AA").value != PlateNumber.parse("01A123AA").value
    assert PlateNumber.parse("O1A123AA").format is PlateFormat.FOREIGN


def test_legal_and_foreign_formats() -> None:
    assert PlateNumber.parse("01 123 AAA").format is PlateFormat.UZ_LEGAL
    assert PlateNumber.parse("01123AAA").value == "01123AAA"
    assert PlateNumber.parse("KZ 123 ABC").format is PlateFormat.FOREIGN
    assert PlateNumber.parse("AB1234").format is PlateFormat.FOREIGN


@pytest.mark.parametrize("raw", ["", "   ", "12", "A" * 13, "01A123A!", "Ж123"])
def test_invalid_plates_raise_without_leaking_value(raw: str) -> None:
    with pytest.raises(InvalidPlateError) as exc:
        PlateNumber.parse(raw)
    assert raw.strip() not in str(exc.value) or raw.strip() == ""


def test_repr_and_str_are_masked() -> None:
    plate = PlateNumber.parse("01A123AA")
    assert str(plate) == "01A***AA"
    assert repr(plate) == "PlateNumber(01A***AA, UZ_STANDARD)"
    assert "123" not in f"{plate}" and "123" not in repr(plate)
    assert mask("AB12") == "A***"
    assert mask("01123AAA") == "011***AA"


def test_equality_by_normalized_value() -> None:
    assert PlateNumber.parse("01 A 123 AA") == PlateNumber.parse("01a123aa")
    assert hash(PlateNumber.parse("01 A 123 AA")) == hash(PlateNumber.parse("01a123aa"))
