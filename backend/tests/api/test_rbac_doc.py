"""docs/RBAC.md совпадает с кодом (права ролей) и OpenAPI (права эндпоинтов): матрица в документации не устаревает."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def test_rbac_doc_is_up_to_date() -> None:
    sys.path.insert(0, str(BACKEND / "scripts"))
    import export_rbac

    assert export_rbac.OUT.read_text(encoding="utf-8") == export_rbac.render(), (
        "docs/RBAC.md устарел: uv run python scripts/export_rbac.py"
    )
