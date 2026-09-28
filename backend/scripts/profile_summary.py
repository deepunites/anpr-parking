"""Сводка профиля py-spy (speedscope JSON) в markdown: самые «горячие» функции и распределение по библиотекам.

Использование: uv run python scripts/profile_summary.py .e2e/api-profile.speedscope.json [--top 20]
Профиль снимается прогоном стенда с PARKING_E2E_PROFILE=1 (scripts/e2e_stack.py) во время k6.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

CATEGORIES = [  # порядок важен: первое совпадение по пути кадра
    ("parking", r"/src/parking/"),
    ("sqlalchemy", r"/sqlalchemy/"),
    ("asyncpg", r"/asyncpg/"),
    ("pydantic", r"/pydantic"),
    ("fastapi/starlette", r"/(fastapi|starlette)/"),
    ("opentelemetry", r"/opentelemetry/"),
    ("redis", r"/redis/"),
    ("uvicorn/h11", r"/(uvicorn|h11|httptools)/"),
    ("asyncio (ожидание)", r"/asyncio/|/selectors\.py|/concurrent/"),
    ("anyio", r"/anyio/"),
]


def _category(file: str) -> str:
    for name, pattern in CATEGORIES:
        if re.search(pattern, file):
            return name
    return "прочее"


def summarize(path: Path, top: int, label: str | None = None) -> str:
    data = json.loads(path.read_text())
    frames = data["shared"]["frames"]
    self_time: Counter[int] = Counter()
    total_time: Counter[int] = Counter()
    leaf_category: Counter[str] = Counter()
    samples = 0
    for profile in data["profiles"]:
        weights = profile.get("weights") or [1] * len(profile["samples"])
        for stack, weight in zip(profile["samples"], weights, strict=True):
            if not stack:
                continue
            samples += weight
            self_time[stack[-1]] += weight
            leaf_category[_category(frames[stack[-1]].get("file") or "")] += weight
            for idx in set(stack):
                total_time[idx] += weight

    def row(idx: int, weight: float) -> str:
        frame = frames[idx]
        file = (frame.get("file") or "?").split("/site-packages/")[-1].split("/src/")[-1]
        return f"| `{frame['name']}` | {file}:{frame.get('line')} | {100 * weight / samples:.1f} % |"

    lines = [
        f"Профиль `{label or path.name}`: {samples:.1f} с активного времени (py-spy, все потоки; простой не учитывается).",
        "",
        "Где проводит время процесс API (по вершине стека):",
        "",
        "| Категория | Доля |",
        "|---|---|",
    ]
    lines += [f"| {name} | {100 * w / samples:.1f} % |" for name, w in leaf_category.most_common()]
    lines += ["", f"Топ-{top} функций по собственному времени:", "", "| Функция | Файл | Доля |", "|---|---|---|"]
    lines += [row(idx, w) for idx, w in self_time.most_common(top)]
    parking_total = [
        (idx, w) for idx, w in total_time.most_common() if "/src/parking/" in (frames[idx].get("file") or "")
    ]
    lines += [
        "",
        f"Топ-{top} функций проекта по суммарному (inclusive) времени:",
        "",
        "| Функция | Файл | Доля |",
        "|---|---|---|",
    ]
    lines += [row(idx, w) for idx, w in parking_total[:top]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None, help="куда записать markdown (иначе stdout)")
    parser.add_argument("--label", default=None, help="подпись профиля в заголовке")
    args = parser.parse_args()
    text = summarize(args.profile, args.top, args.label)
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
