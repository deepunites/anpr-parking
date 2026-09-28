"""Генерирует docs/RBAC.md: матрица «роль × право» из modules/identity/domain/permissions.py и «эндпоинт → право»
из OpenAPI (x-permission / x-public / x-camera-auth). Код первичен; тест tests/api/test_rbac_doc.py сверяет файл."""

from __future__ import annotations

import json
from pathlib import Path

from parking.modules.identity.domain.permissions import ROLE_PERMISSIONS, Permission, Role

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "RBAC.md"
SPEC = ROOT / "docs" / "openapi.json"
ORDER = [
    Role.VIEWER,
    Role.OPERATOR,
    Role.CASHIER,
    Role.MANAGER,
    Role.TENANT_ADMIN,
    Role.OWNER,
    Role.DEVELOPER,
    Role.PLATFORM_ADMIN,
    Role.PLATFORM_OWNER,
]


def render() -> str:
    roles = [r for r in ORDER if r in ROLE_PERMISSIONS] + [r for r in Role if r not in ORDER]
    lines = [
        "# Матрица прав (RBAC)",
        "",
        "Сгенерировано из `backend/src/parking/modules/identity/domain/permissions.py` и `docs/openapi.json`",
        "(`uv run python scripts/export_rbac.py`). Не редактировать вручную. Контрактный тест",
        "`tests/api/test_rbac_contract.py` вызывает каждый эндпоинт каждой ролью и проверяет 403/не-403 по этой матрице;",
        "API-ключ внешней системы получает только права из своих scopes (не роль создателя, A-89).",
        "",
        "## Роли × права",
        "",
        "| Право | " + " | ".join(r.value for r in roles) + " |",
        "|---|" + "---|" * len(roles),
    ]
    for perm in Permission:
        cells = ["✓" if perm in ROLE_PERMISSIONS[r] else "" for r in roles]
        lines.append(f"| `{perm.value}` | " + " | ".join(cells) + " |")
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    lines += ["", "## Эндпоинты", "", "| Метод | Путь | Доступ |", "|---|---|---|"]
    for path, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            if op.get("x-camera-auth"):
                access = "ключ камеры (`X-Camera-Key`)"
            elif op.get("x-public"):
                access = "публичный"
            else:
                access = ", ".join(f"`{p}`" for p in op.get("x-permission", [])) or "—"
            lines.append(f"| {method.upper()} | `{path}` | {access} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"written {OUT}")
