#!/usr/bin/env bash
# Gate v2 (этап 8 «Приёмка»): все проверки подряд, вывод — docs/backend/gate-v2.out.
# Локальная среда без root/Docker: uv, Node, helm, promtool, gitleaks — в ~/.local (см. README).
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(cd .. && pwd)
OUT="$ROOT/docs/backend/gate-v2.out"
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$HOME/.local/prometheus/bin:$PATH"
FAILED=()

step() {
  local name=$1; shift
  echo "" | tee -a "$OUT"
  echo "\$ $*" | tee -a "$OUT"
  if ( "$@" ) 2>&1 | grep -v '^{"' | tail -n "${TAIL:-12}" | tee -a "$OUT"; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    echo "[OK] $name" | tee -a "$OUT"
  else
    echo "[FAIL] $name" | tee -a "$OUT"
    FAILED+=("$name")
  fi
}

echo "# Gate v2 (этап 8 «Приёмка») — $(date -u +%Y-%m-%dT%H:%MZ), рабочая станция без root/Docker (PostgreSQL 16 pgserver, Redis 7, MinIO; helm/promtool/gitleaks в ~/.local)" > "$OUT"

step "ruff" bash -c "uv run ruff check . && uv run ruff format --check ."
step "mypy --strict" uv run mypy src
step "openapi export" uv run python scripts/export_openapi.py
step "rbac doc" uv run python scripts/export_rbac.py
TAIL=25 step "pytest (весь набор)" uv run pytest -q -p no:cacheprovider
step "retention.sql под ролью maintenance" uv run python - <<'EOF'
import asyncio, sys, asyncpg
sys.path.insert(0, "tests")
from conftest import PGDATA
async def main():
    c = await asyncpg.connect(f"postgresql://app_maintenance@/parking_test?host={PGDATA}")
    print(await c.execute(open("scripts/retention.sql").read()))
asyncio.run(main())
EOF
step "promtool rules" promtool check rules "$ROOT/deploy/prometheus/alerts.yaml"
step "helm lint" "$HOME/.local/tools/helm" lint "$ROOT/deploy/helm/parking"
step "helm template (sandbox)" bash -c "$HOME/.local/tools/helm template t $ROOT/deploy/helm/parking -f $ROOT/deploy/helm/parking/values-sandbox.yaml | grep -c '^kind:'"
step "gitleaks (всё дерево, конфиг security/gitleaks.toml)" bash -c "cd $ROOT && $HOME/.local/tools/gitleaks dir --no-banner --config security/gitleaks.toml ."
cd "$ROOT/frontend"
step "frontend gen:api (без расхождений)" bash -c "npm run -s gen:api >/dev/null && echo schema.d.ts regenerated"
step "frontend typecheck" npm run -s typecheck
step "frontend lint" npm run -s lint
step "frontend vitest" npx vitest run
step "frontend build" npm run -s build
TAIL=60 step "E2E (Playwright, все спецификации)" npx playwright test

echo "" | tee -a "$OUT"
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "GATE v2: PASS" | tee -a "$OUT"
else
  echo "GATE v2: FAIL — ${FAILED[*]}" | tee -a "$OUT"
  exit 1
fi
