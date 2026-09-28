"""Домен схемы площадки: генерация мест, лимиты и ошибки документа, зона камеры (вложенная зона важнее)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from parking.modules.tenancy.domain.layout import LotLayout
from parking.shared.errors import ValidationError


def test_nested_zone_wins_for_camera() -> None:
    cam = uuid4()
    layout = LotLayout.parse(
        {
            "width": 1000,
            "height": 800,
            "zones": [
                {"id": "all", "name": "Вся", "x": 0, "y": 0, "w": 1000, "h": 800},
                {"id": "vip", "name": "VIP", "x": 100, "y": 100, "w": 200, "h": 200},
            ],
            "devices": [{"id": "c", "kind": "camera", "ref_id": str(cam), "x": 150, "y": 150}],
        }
    )
    assert layout.camera_zones() == {cam: "vip"}


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"width": 50}, "width"),
        ({"zones": [{"id": "bad id!", "name": "x", "x": 0, "y": 0, "w": 10, "h": 10}]}, "element id"),
        ({"rows": [{"id": "r", "x": 0, "y": 0, "count": 201}]}, "count"),
        ({"rows": [{"id": "r", "x": 0, "y": 0, "count": 2, "prefix": "a b"}]}, "prefix"),
        ({"labels": [{"id": "l", "x": 0, "y": 0, "text": "  "}]}, "text"),
        ({"zones": [{"id": "z", "name": "Z", "x": 0, "y": 0, "w": 10, "h": 10, "color": "pink"}]}, "color"),
    ],
)
def test_invalid_documents_are_rejected(patch: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        LotLayout.parse({"width": 1000, "height": 800, **patch})


def test_too_many_spots() -> None:
    rows = [{"id": f"r{i}", "x": 0, "y": 0, "count": 200, "prefix": f"P{i}-"} for i in range(26)]
    with pytest.raises(ValidationError, match="too many spots"):
        LotLayout.parse({"width": 1000, "height": 800, "rows": rows})


def test_round_trip_is_stable() -> None:
    raw = {
        "width": 800,
        "height": 600,
        "grid": 10,
        "zones": [{"id": "z", "name": "Z", "color": "teal", "x": 10, "y": 10, "w": 300, "h": 200, "capacity": 7}],
        "rows": [
            {"id": "r", "zone_id": "z", "x": 20, "y": 20, "count": 3, "direction": "v", "prefix": "Q", "start": 5}
        ],
        "roads": [],
        "devices": [],
        "labels": [],
    }
    layout = LotLayout.parse(raw)
    assert [s.code for s in layout.spots] == ["Q5", "Q6", "Q7"]
    assert layout.zone_capacity(layout.zones[0]) == 7
    assert LotLayout.parse(layout.to_dict()).to_dict() == layout.to_dict()
