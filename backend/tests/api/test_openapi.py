"""docs/openapi.json генерируется из кода и обязан совпадать с зафиксированным (расхождение — перегенерировать)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from tests.conftest import BACKEND

SPEC = BACKEND.parent / "docs" / "openapi.json"


def test_committed_openapi_matches_application(app: FastAPI) -> None:
    generated = json.loads(json.dumps(app.openapi(), sort_keys=True))
    committed = json.loads(SPEC.read_text(encoding="utf-8"))
    assert generated == committed, "docs/openapi.json устарел: uv run python scripts/export_openapi.py"
    assert committed["openapi"] == "3.1.0"


def test_compat_checker_detects_breaking_changes() -> None:
    import subprocess
    import sys

    committed = json.loads(SPEC.read_text(encoding="utf-8"))
    broken = json.loads(json.dumps(committed))
    del broken["paths"]["/api/v1/lots"]
    tmp = Path(BACKEND / ".pytest_cache" / "openapi_broken.json")
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps(broken))
    result = subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "openapi_compat.py"), str(SPEC), str(tmp)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and "removed operation GET /api/v1/lots" in result.stdout
    same = subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "openapi_compat.py"), str(SPEC), str(SPEC)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert same.returncode == 0
