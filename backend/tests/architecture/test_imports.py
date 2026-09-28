"""Архитектурный тест (фаза 3): границы слоёв и модулей проверяются обходом AST всех файлов src/.

Правила:
  shared        → только stdlib/третьи стороны без I/O-клиентов и parking.shared
  platform      → shared, platform; никаких modules/api/workers
  domain        → shared, domain того же модуля; ни platform, ни application/infrastructure, ни клиентов I/O
  application   → + domain/application того же модуля, platform (кроме platform.http), application.contracts других модулей
  infrastructure→ + всё своего модуля, platform; чужие модули только через application.contracts
  api, workers, runtime → application/infrastructure любых модулей (composition root), platform, shared; не domain
  httpx         → только platform.http и */infrastructure (I-9 статически)
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = "parking"
IO_CLIENTS = {"httpx", "aiohttp", "requests", "redis", "asyncpg", "sqlalchemy", "celery", "aioboto3", "botocore"}
CONTRACTS = ".application.contracts"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path, module: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = (
                    module.split(".")[: -node.level]
                    if path.name != "__init__.py"
                    else module.split(".")[: len(module.split(".")) - node.level + 1]
                )
                prefix = ".".join(base)
                found.append(f"{prefix}.{node.module}" if node.module else prefix)
            elif node.module:
                found.append(node.module)
    return found


def _layer(module: str) -> tuple[str, str | None, str | None]:
    """→ (zone, context, layer): zone ∈ shared|platform|modules|api|workers|root."""
    parts = module.split(".")
    if len(parts) < 2:
        return "root", None, None
    zone = parts[1]
    if zone == "modules" and len(parts) >= 4:
        return "modules", parts[2], parts[3]
    if zone == "modules":
        return "modules", parts[2] if len(parts) > 2 else None, None
    return zone, None, None


def _violation(module: str, imported: str) -> str | None:  # noqa: PLR0911, PLR0912 — таблица правил читается сверху вниз
    zone, ctx, layer = _layer(module)
    top = imported.split(".", maxsplit=1)[0]
    if top != PKG:
        if top in IO_CLIENTS:
            allowed = zone in {"platform", "api", "workers", "runtime"} or layer == "infrastructure"
            if top == "httpx":
                allowed = module.startswith("parking.platform.http") or layer == "infrastructure"
            if not allowed:
                return f"{module} imports I/O client {imported}"
        return None
    izone, ictx, ilayer = _layer(imported)
    if zone == "shared":
        return None if izone == "shared" else f"shared → {imported}"
    if zone == "platform":
        return None if izone in {"shared", "platform"} else f"platform → {imported}"
    if zone == "modules":
        if izone in {"shared"}:
            return None
        if izone == "platform":
            if layer == "domain":
                return f"domain → platform: {module} imports {imported}"
            if layer == "application" and imported.startswith("parking.platform.http"):
                return f"application → platform.http: {module}"
            return None
        if izone == "modules":
            if ictx == ctx:
                if layer == "domain" and ilayer != "domain":
                    return f"domain → {ilayer}: {module} imports {imported}"
                if layer == "application" and ilayer == "infrastructure":
                    return f"application → infrastructure: {module} imports {imported}"
                return None
            if CONTRACTS in imported:
                return None
            return f"cross-module import {module} → {imported} (only application.contracts allowed)"
        return f"{module} imports {imported}"
    if zone in {"api", "workers", "runtime"}:
        if izone == "modules" and ilayer == "domain":
            return f"{zone} → domain: {module} imports {imported}"
        return None
    return None


def test_import_rules() -> None:
    violations: list[str] = []
    for path in sorted((SRC / PKG).rglob("*.py")):
        module = _module_name(path)
        for imported in _imports(path, module):
            v = _violation(module, imported)
            if v:
                violations.append(v)
    assert violations == [], "\n".join(violations)


def test_every_module_has_three_layers_or_is_placeholder() -> None:
    modules_dir = SRC / PKG / "modules"
    for ctx in sorted(p for p in modules_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        layers = {p.name for p in ctx.iterdir() if p.is_dir()}
        assert layers <= {"domain", "application", "infrastructure", "__pycache__"}, (ctx.name, layers)
