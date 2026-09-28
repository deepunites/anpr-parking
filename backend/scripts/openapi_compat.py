"""Проверка обратной совместимости OpenAPI v1: `python scripts/openapi_compat.py old.json new.json`.

Несовместимо (код выхода 1): удалённый путь/метод, новый обязательный параметр или поле запроса, удалённое поле ответа,
изменение типа поля. Заменяет oasdiff там, где бинарник недоступен; в CI (фаза 9) используется oasdiff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _ops(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (path, method.upper()): op for path, methods in spec.get("paths", {}).items() for method, op in methods.items()
    }


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return spec["components"]["schemas"][name]  # type: ignore[no-any-return]
    return schema


def _props(spec: dict[str, Any], schema: dict[str, Any] | None) -> tuple[dict[str, Any], set[str]]:
    if not schema:
        return {}, set()
    resolved = _resolve(spec, schema)
    return resolved.get("properties", {}), set(resolved.get("required", []))


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    breaking: list[str] = []
    old_ops, new_ops = _ops(old), _ops(new)
    for key, op in old_ops.items():
        if key not in new_ops:
            breaking.append(f"removed operation {key[1]} {key[0]}")
            continue
        new_op = new_ops[key]
        old_params = {(p["name"], p["in"]) for p in op.get("parameters", [])}
        for p in new_op.get("parameters", []):
            if p.get("required") and (p["name"], p["in"]) not in old_params:
                breaking.append(f"{key[1]} {key[0]}: new required parameter {p['name']}")
        old_body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        new_body = new_op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        _, old_req = _props(old, old_body)
        _, new_req = _props(new, new_body)
        for field in new_req - old_req:
            if old_body is not None:
                breaking.append(f"{key[1]} {key[0]}: request field {field} became required")
        for status, response in op.get("responses", {}).items():
            new_response = new_op.get("responses", {}).get(status)
            if new_response is None:
                breaking.append(f"{key[1]} {key[0]}: response {status} removed")
                continue
            old_schema = response.get("content", {}).get("application/json", {}).get("schema")
            new_schema = new_response.get("content", {}).get("application/json", {}).get("schema")
            o_props, _ = _props(old, old_schema)
            n_props, _ = _props(new, new_schema)
            for field, definition in o_props.items():
                if field not in n_props:
                    breaking.append(f"{key[1]} {key[0]}: response {status} field {field} removed")
                elif definition.get("type") != n_props[field].get("type"):
                    breaking.append(f"{key[1]} {key[0]}: response {status} field {field} changed type")
    return breaking


def main() -> int:
    old = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    new = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    problems = compare(old, new)
    for p in problems:
        print(f"BREAKING: {p}")
    print("compatible" if not problems else f"{len(problems)} breaking change(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
