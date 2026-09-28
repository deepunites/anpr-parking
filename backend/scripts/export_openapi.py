"""Выгружает OpenAPI 3.1 в docs/openapi.json (CI сравнивает с зафиксированным и проверяет совместимость)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from parking.api.app import create_app
from parking.platform.config import DevSettings

OUT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"


def spec() -> dict[str, object]:
    os.environ.setdefault("PARKING_ENV", "dev")
    app = create_app(DevSettings(_env_file=None))  # type: ignore[call-arg]
    return app.openapi()


if __name__ == "__main__":
    OUT.write_text(json.dumps(spec(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"written {OUT}")
