#!/usr/bin/env python3
# ruff: noqa: T201, S603 — CLI-скрипт: печать отчёта, запуск известных бинарников
"""Сканеры безопасности фазы 8 §7 одной командой (локально и в CI): bandit, pip-audit, npm audit, trivy fs,
gitleaks, semgrep. Критическая уязвимость — ненулевой код возврата; принятые риски — security/exceptions.yaml
(исключение с истёкшим review_by тоже роняет прогон).

    backend/.venv/bin/python scripts/security_scan.py [--skip semgrep,trivy]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(os.environ.get("PARKING_TOOLS", Path.home() / ".local" / "tools"))
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
NPM = shutil.which("npm") or str(Path.home() / ".local/node/bin/npm")
SCRATCH = Path(os.environ.get("PARKING_SCAN_OUT", ROOT / "backend" / ".security-scan"))
SKIP_DIRS = [
    "backend/.venv",
    "backend/mutants",
    "frontend/node_modules",
    "backend/.pgdata",
    "backend/.failure-lab",
    "backend/.e2e",
    "backend/.minio-data",
    "backend/.redis-data",
    "frontend/dist",
    "frontend/test-results",
    "backend/.security-scan",
    ".claude",  # локальные worktree агентов (в .gitignore, в CI их нет)
]


def run(cmd: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{Path(NPM).parent}:{Path(UV).parent}:{os.environ.get('PATH', '')}"}
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False, env=env)


def check_exceptions() -> list[str]:
    data = yaml.safe_load((ROOT / "security" / "exceptions.yaml").read_text())
    problems = []
    today = dt.date.today()
    for exc in data["exceptions"]:
        for key in ("id", "tools", "rules", "justification", "accepted_on", "review_by", "owner"):
            if not exc.get(key):
                problems.append(f"{exc.get('id', '?')}: нет поля {key}")
        if isinstance(exc.get("review_by"), dt.date) and exc["review_by"] < today:
            problems.append(f"{exc['id']}: срок пересмотра истёк {exc['review_by']}")
    return problems


def bandit() -> tuple[bool, str]:
    r = run(
        [UV, "run", "bandit", "-q", "-c", "../security/bandit.yaml", "-r", "src", "-f", "json"], cwd=ROOT / "backend"
    )
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return False, r.stderr[-400:]
    issues = [i for i in data["results"] if i["issue_severity"] in {"HIGH", "MEDIUM"}]
    return not issues, f"{len(data['results'])} issues total, {len(issues)} medium/high"


def pip_audit() -> tuple[bool, str]:
    req = SCRATCH / "requirements.txt"
    exp = run(
        [UV, "export", "--no-dev", "--no-hashes", "--no-emit-project", "-q", "-o", str(req)], cwd=ROOT / "backend"
    )
    if exp.returncode != 0:
        return False, exp.stderr[-300:]
    r = run([UV, "run", "pip-audit", "--strict", "-r", str(req)], cwd=ROOT / "backend")
    output = (r.stdout + r.stderr).strip()
    return r.returncode == 0, output.splitlines()[-1] if output else "ok"


def npm_audit() -> tuple[bool, str]:
    r = run([NPM, "audit", "--json"], cwd=ROOT / "frontend")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return False, r.stderr[-300:]
    counts = data["metadata"]["vulnerabilities"]
    blocking = counts.get("critical", 0) + counts.get("high", 0)
    return blocking == 0, ", ".join(f"{k}={v}" for k, v in counts.items())


def tool(name: str) -> Path:
    """Бинарник сканера: из PATH (CI ставит его action'ом) или из ~/.local/tools (локально)."""
    found = shutil.which(name)
    return Path(found) if found else TOOLS / name


def trivy() -> tuple[bool, str]:
    binary = tool("trivy")
    if not binary.exists():
        return False, f"нет trivy в PATH и {binary}"
    r = run(
        [
            str(binary),
            "fs",
            "--scanners",
            "vuln,secret,misconfig",
            "--skip-dirs",
            ",".join(SKIP_DIRS),
            "--severity",
            "CRITICAL,HIGH",
            "--exit-code",
            "1",
            "--quiet",
            "--format",
            "json",
            ".",
        ]
    )
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return False, r.stderr[-300:]
    findings = 0
    for result in data.get("Results", []) or []:
        findings += len(result.get("Vulnerabilities") or []) + len(result.get("Secrets") or [])
        findings += len(result.get("Misconfigurations") or [])
    return r.returncode == 0 and findings == 0, f"{findings} critical/high findings"


def gitleaks() -> tuple[bool, str]:
    binary = tool("gitleaks")
    if not binary.exists():
        return False, f"нет gitleaks в PATH и {binary}"
    r = run(
        [str(binary), "dir", ".", "--config", "security/gitleaks.toml", "--no-banner", "--redact", "--exit-code", "1"]
    )
    return r.returncode == 0, (r.stderr.strip().splitlines() or ["ok"])[-1][-80:]


def semgrep() -> tuple[bool, str]:
    uvx = shutil.which("uvx") or str(Path.home() / ".local/bin/uvx")
    r = run(
        [
            uvx,
            "semgrep",
            "scan",
            "--config",
            "p/python",
            "--config",
            "p/fastapi",
            "--config",
            "p/react",
            "--config",
            "p/typescript",
            "--metrics=off",
            "--quiet",
            "--json",
            "--exclude",
            "node_modules",
            "--exclude",
            ".venv",
            "--exclude",
            "mutants",
            "--exclude",
            "dist",
            "backend/src",
            "frontend/src",
        ]
    )
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return False, r.stderr[-300:]
    exceptions = yaml.safe_load((ROOT / "security/exceptions.yaml").read_text())["exceptions"]
    accepted = {rule for exc in exceptions for rule in exc["rules"]}
    blocking = [f for f in data["results"] if f["check_id"] not in accepted and f["extra"]["severity"] == "ERROR"]
    return not blocking, f"{len(data['results'])} findings, {len(blocking)} blocking (не в exceptions.yaml)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip", default="", help="через запятую: bandit,pip-audit,npm-audit,trivy,gitleaks,semgrep")
    args = parser.parse_args()
    skipped = set(filter(None, args.skip.split(",")))
    SCRATCH.mkdir(parents=True, exist_ok=True)
    ok = True
    problems = check_exceptions()
    print(f"exceptions.yaml: {'ok' if not problems else '; '.join(problems)}")
    ok &= not problems
    scanners = (
        ("bandit", bandit),
        ("pip-audit", pip_audit),
        ("npm-audit", npm_audit),
        ("trivy", trivy),
        ("gitleaks", gitleaks),
        ("semgrep", semgrep),
    )
    for name, fn in scanners:
        if name in skipped:
            print(f"{name:10s} SKIPPED")
            continue
        passed, detail = fn()
        ok &= passed
        print(f"{name:10s} {'PASS' if passed else 'FAIL'}  {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
