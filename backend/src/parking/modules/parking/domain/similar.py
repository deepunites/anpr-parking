"""Похожесть номеров для «выезда без въезда»: камера выезда прочитала номер с ошибкой (MediaPark, 2026-10-15, ночь:
90D077DD → 90D07CD, 90F215BB → F2155BB, 90A557WA → A0A557AA), и выезд не нашёл въезд. Кассиру показываются машины на
площадке с похожим номером — он сверяет фото и выбирает «это она», а не ищет номер вручную.

Расстояние — правка Левенштейна с весами: путаница распознавания (O/0/D/Q, B/8, S/5, Z/2…) — половина правки,
потерянные или лишние символы в начале номера (код региона «90» обрезан кадром) — половина за символ."""

from __future__ import annotations

SIMILAR_MAX = 3.0  # дальше — уже другая машина; кассир видит лучшие кандидаты первыми

_CONFUSED = (
    "O0DQU",
    "B8",
    "S5",
    "Z27",
    "I1TL",
    "G6C",
    "A4",
    "HN",
    "PR",
    "E3F",
    "MW",
    "VY",
    "XK",
)
_PAIRS = frozenset((a, b) for group in _CONFUSED for a in group for b in group if a != b)


def _substitution(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return 0.5 if (a, b) in _PAIRS else 1.0


def plate_distance(a: str, b: str) -> float:
    """Взвешенная правка между номерами (A-Z0-9, без пробелов): 0 — совпадают, ≤ SIMILAR_MAX — похожи."""
    prev = [0.5 * j for j in range(len(b) + 1)]  # символы в начале номера — половина правки
    for i, ca in enumerate(a, start=1):
        cur = [0.5 * i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + _substitution(ca, cb)))
        prev = cur
    return prev[-1]
