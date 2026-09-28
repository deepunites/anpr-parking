"""Схема площадки (v2 этап 5): зоны, ряды мест, проезды, устройства, подписи — в координатах холста.

Документ приходит от редактора как JSON и проверяется здесь целиком: размеры, лимиты, уникальность id и кодов мест,
ссылки ряд → зона и устройство → зона. Места не хранятся по одному — их порождают ряды (префикс + номер), так схема
на 500 мест остаётся компактной. Зона камеры въезда определяет, в какую зону засчитывается въехавшая машина.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from parking.shared.errors import ValidationError

MAX_SIZE = 5000
MAX_ELEMENTS = 500
MAX_SPOTS = 5000
MAX_ROW = 200
COLORS = ("blue", "green", "amber", "violet", "rose", "teal", "gray")
DEVICE_KINDS = ("camera", "barrier")
LABEL_KINDS = ("entry", "exit", "text")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PREFIX = re.compile(r"^[A-Z0-9-]{0,6}$")


@dataclass(frozen=True, slots=True)
class Zone:
    id: str
    name: str
    color: str
    x: int
    y: int
    w: int
    h: int
    capacity: int | None = None  # None — по числу мест в рядах зоны

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


@dataclass(frozen=True, slots=True)
class Row:
    id: str
    x: int
    y: int
    count: int
    direction: str  # "h" — места вправо, "v" — вниз
    prefix: str
    start: int
    spot_w: int
    spot_h: int
    zone_id: str | None = None


@dataclass(frozen=True, slots=True)
class Road:
    id: str
    x: int
    y: int
    w: int
    h: int
    label: str | None = None


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    kind: str
    ref_id: UUID
    x: int
    y: int
    zone_id: str | None = None


@dataclass(frozen=True, slots=True)
class Label:
    id: str
    kind: str
    x: int
    y: int
    text: str


@dataclass(frozen=True, slots=True)
class Spot:
    code: str
    zone_id: str | None
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True, slots=True)
class LotLayout:
    width: int
    height: int
    grid: int
    zones: tuple[Zone, ...] = ()
    rows: tuple[Row, ...] = ()
    roads: tuple[Road, ...] = ()
    devices: tuple[Device, ...] = ()
    labels: tuple[Label, ...] = ()
    _spots: tuple[Spot, ...] = field(default=(), compare=False, repr=False)

    # ---------- разбор ----------
    @classmethod
    def parse(cls, raw: dict[str, Any]) -> LotLayout:
        if not isinstance(raw, dict):
            raise _bad("layout must be an object")
        width, height = _int(raw, "width", 100, MAX_SIZE), _int(raw, "height", 100, MAX_SIZE)
        grid = _int(raw, "grid", 1, 200, default=10)

        def items(key: str) -> list[dict[str, Any]]:
            value = raw.get(key) or []
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                raise _bad(f"{key} must be a list of objects")
            return value

        zones = tuple(
            Zone(
                id=_id(z),
                name=_text(z, "name", 30),
                color=_choice(z, "color", COLORS, default="blue"),
                x=_int(z, "x", 0, width),
                y=_int(z, "y", 0, height),
                w=_int(z, "w", 10, width),
                h=_int(z, "h", 10, height),
                capacity=_int(z, "capacity", 0, 100_000) if z.get("capacity") is not None else None,
            )
            for z in items("zones")
        )
        zone_ids = {z.id for z in zones}
        rows = tuple(
            Row(
                id=_id(r),
                x=_int(r, "x", 0, width),
                y=_int(r, "y", 0, height),
                count=_int(r, "count", 1, MAX_ROW),
                direction=_choice(r, "direction", ("h", "v"), default="h"),
                prefix=_prefix(r),
                start=_int(r, "start", 0, 99_999, default=1),
                spot_w=_int(r, "spot_w", 8, 200, default=25),
                spot_h=_int(r, "spot_h", 8, 200, default=50),
                zone_id=_ref(r, "zone_id", zone_ids),
            )
            for r in items("rows")
        )
        roads = tuple(
            Road(
                id=_id(d),
                x=_int(d, "x", 0, width),
                y=_int(d, "y", 0, height),
                w=_int(d, "w", 5, width),
                h=_int(d, "h", 5, height),
                label=_text(d, "label", 40) if d.get("label") else None,
            )
            for d in items("roads")
        )
        devices = tuple(
            Device(
                id=_id(d),
                kind=_choice(d, "kind", DEVICE_KINDS),
                ref_id=_uuid(d, "ref_id"),
                x=_int(d, "x", 0, width),
                y=_int(d, "y", 0, height),
                zone_id=_ref(d, "zone_id", zone_ids),
            )
            for d in items("devices")
        )
        labels = tuple(
            Label(
                id=_id(lb),
                kind=_choice(lb, "kind", LABEL_KINDS, default="text"),
                x=_int(lb, "x", 0, width),
                y=_int(lb, "y", 0, height),
                text=_text(lb, "text", 40),
            )
            for lb in items("labels")
        )
        all_ids = [e.id for group in (zones, rows, roads, devices, labels) for e in group]
        if len(all_ids) > MAX_ELEMENTS:
            raise _bad(f"too many elements (max {MAX_ELEMENTS})")
        if len(all_ids) != len(set(all_ids)):
            raise _bad("element ids must be unique")
        refs = [(d.kind, d.ref_id) for d in devices]
        if len(refs) != len(set(refs)):
            raise _bad("each camera or barrier can be placed once")
        layout = cls(
            width=width, height=height, grid=grid, zones=zones, rows=rows, roads=roads, devices=devices, labels=labels
        )
        spots = layout._generate_spots()
        object.__setattr__(layout, "_spots", spots)
        return layout

    # ---------- вычисления ----------
    def _generate_spots(self) -> tuple[Spot, ...]:
        spots: list[Spot] = []
        codes: set[str] = set()
        for row in self.rows:
            for i in range(row.count):
                code = f"{row.prefix}{row.start + i}"
                if len(code) > 20:
                    raise _bad(f"spot code too long: {code}")
                if code in codes:
                    raise _bad(f"duplicate spot code {code}: change the prefix or start number of row {row.id}")
                codes.add(code)
                dx, dy = (row.spot_w * i, 0) if row.direction == "h" else (0, row.spot_h * i)
                spots.append(Spot(code, row.zone_id, row.x + dx, row.y + dy, row.spot_w, row.spot_h))
                if len(spots) > MAX_SPOTS:
                    raise _bad(f"too many spots (max {MAX_SPOTS})")
        return tuple(spots)

    @property
    def spots(self) -> tuple[Spot, ...]:
        return self._spots

    def zone_capacity(self, zone: Zone) -> int:
        if zone.capacity is not None:
            return zone.capacity
        return sum(1 for s in self._spots if s.zone_id == zone.id)

    def camera_zones(self) -> dict[UUID, str]:
        """Камера → зона: явная привязка или зона, внутри которой стоит значок камеры."""
        result: dict[UUID, str] = {}
        for d in self.devices:
            if d.kind != "camera":
                continue
            inner = sorted((z for z in self.zones if z.contains(d.x, d.y)), key=lambda z: z.w * z.h)
            zone_id = d.zone_id or (inner[0].id if inner else None)  # вложенная зона важнее объемлющей
            if zone_id is not None:
                result[d.ref_id] = zone_id
        return result

    def device_refs(self) -> set[tuple[str, UUID]]:
        return {(d.kind, d.ref_id) for d in self.devices}

    def to_dict(self) -> dict[str, Any]:
        def clean(obj: Any) -> dict[str, Any]:
            data = {k: getattr(obj, k) for k in obj.__slots__ if not k.startswith("_")}
            return {k: (str(v) if isinstance(v, UUID) else v) for k, v in data.items() if v is not None}

        return {
            "width": self.width,
            "height": self.height,
            "grid": self.grid,
            "zones": [clean(z) for z in self.zones],
            "rows": [clean(r) for r in self.rows],
            "roads": [clean(r) for r in self.roads],
            "devices": [clean(d) for d in self.devices],
            "labels": [clean(lb) for lb in self.labels],
        }


EMPTY_LAYOUT: dict[str, Any] = {
    "width": 1200,
    "height": 800,
    "grid": 10,
    "zones": [],
    "rows": [],
    "roads": [],
    "devices": [],
    "labels": [],
}


def _bad(message: str) -> ValidationError:
    return ValidationError(message, code="INVALID_LAYOUT")


def _int(d: dict[str, Any], key: str, lo: int, hi: int, *, default: int | None = None) -> int:
    value = d.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _bad(f"{key} must be a number")
    number = round(value)
    if not lo <= number <= hi:
        raise _bad(f"{key} must be between {lo} and {hi}")
    return number


def _text(d: dict[str, Any], key: str, max_len: int) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_len:
        raise _bad(f"{key} must be 1..{max_len} characters")
    return value.strip()


def _id(d: dict[str, Any]) -> str:
    value = d.get("id")
    if not isinstance(value, str) or not _ID.match(value):
        raise _bad("element id must be 1..40 letters, digits, '-' or '_'")
    return value


def _choice(d: dict[str, Any], key: str, options: tuple[str, ...], *, default: str | None = None) -> str:
    value = d.get(key, default)
    if value not in options:
        raise _bad(f"{key} must be one of {', '.join(options)}")
    return str(value)


def _prefix(d: dict[str, Any]) -> str:
    value = str(d.get("prefix", "")).upper()
    if not _PREFIX.match(value):
        raise _bad("row prefix must be up to 6 letters, digits or '-'")
    return value


def _ref(d: dict[str, Any], key: str, known: set[str]) -> str | None:
    value = d.get(key)
    if value in (None, ""):
        return None
    if value not in known:
        raise _bad(f"{key} refers to an unknown zone: {value}")
    return str(value)


def _uuid(d: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(d.get(key)))
    except ValueError:
        raise _bad(f"{key} must be a UUID") from None
