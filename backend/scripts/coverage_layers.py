"""Покрытие по слоям (фаза 8 §1): читает coverage.json и проверяет пороги таблицы §1; печатает таблицу для gate.

uv run pytest -q --cov=parking --cov-report=json:coverage.json
uv run python scripts/coverage_layers.py coverage.json [--markdown docs/backend/coverage-layers.md]
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# (регэксп по пути файла, порог %, описание). Первое совпадение выигрывает.
LAYERS: list[tuple[str, float, str]] = [
    (r"modules/tariff/domain/", 95, "tariff/domain — деньги"),
    (r"modules/billing/domain/", 95, "billing/domain — деньги"),
    (r"modules/parking/domain/", 90, "parking/domain — сессии, сага"),
    (r"modules/access/domain/", 90, "access/domain — безопасность"),
    (r"modules/[a-z]+/domain/", 85, "прочие */domain"),
    (r"modules/[a-z]+/application/", 85, "*/application"),
    (r"modules/[a-z]+/infrastructure/", 70, "*/infrastructure"),
    (r"parking/api/", 80, "api"),
    (r"parking/(platform|shared|runtime|workers)/", 70, "platform/shared/runtime/workers"),
]
PROJECT_THRESHOLD = 80.0


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "coverage.json")
    data = json.loads(path.read_text())
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    detail: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for file, info in data["files"].items():
        summary = info["summary"]
        for pattern, _threshold, label in LAYERS:
            if re.search(pattern, file):
                totals[label][0] += summary["covered_lines"]
                totals[label][1] += summary["num_statements"]
                module = re.search(r"modules/([a-z]+)/(domain|application|infrastructure)", file)
                if module:
                    key = f"{module.group(1)}/{module.group(2)}"
                    detail[key][0] += summary["covered_lines"]
                    detail[key][1] += summary["num_statements"]
                break
    failed = False
    rows = ["| Слой | Порог | Покрытие | Строк | Статус |", "|---|---|---|---|---|"]
    for _pattern, threshold, label in LAYERS:
        covered, total = totals.get(label, [0, 0])
        if total == 0:
            continue
        pct = 100 * covered / total
        ok = pct >= threshold
        failed |= not ok
        rows.append(f"| {label} | ≥ {threshold:.0f} % | {pct:.1f} % | {covered}/{total} | {'✅' if ok else '❌'} |")
    project = float(data["totals"]["percent_covered"])
    ok = project >= PROJECT_THRESHOLD
    failed |= not ok
    rows.append(f"| **проект** | ≥ {PROJECT_THRESHOLD:.0f} % | **{project:.1f} %** | | {'✅' if ok else '❌'} |")
    per_module = ["", "| Модуль/слой | Покрытие |", "|---|---|"]
    for key, (covered, total) in sorted(detail.items()):
        if total:
            per_module.append(f"| {key} | {100 * covered / total:.1f} % ({covered}/{total}) |")
    text = "\n".join(rows + per_module)
    print(text)
    if "--markdown" in argv:
        out = Path(argv[argv.index("--markdown") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "# Покрытие по слоям (фаза 8 §1)\n\nГенерируется `scripts/coverage_layers.py` из `coverage.json`.\n\n"
            + text
            + "\n"
        )
    print("\nFAILED: пороги §1 не выполнены" if failed else "\nOK: все пороги §1 выполнены")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
