#!/usr/bin/env bash
# Откат релиза одной командой (фаза 9 §5): helm rollback на предыдущую ревизию + смоук. Код и схема совместимы в пределах
# релиза (expand → migrate → contract в трёх релизах, docs/INFRA.md §5), поэтому схема НЕ откатывается: alembic downgrade
# в production запрещён — откат несовместимой миграции = forward-fix новой миграцией.
#   scripts/rollback.sh <release> <namespace> [--drill]   # --drill: откат → смоук → возврат на текущую ревизию (staging)
set -euo pipefail
release="${1:?release}"; ns="${2:?namespace}"; mode="${3:-}"
current=$(helm history "$release" -n "$ns" -o json | python3 -c 'import json,sys; h=[x for x in json.load(sys.stdin) if x["status"]=="deployed"]; print(h[-1]["revision"])')
previous=$(helm history "$release" -n "$ns" -o json | python3 -c 'import json,sys; h=[x for x in json.load(sys.stdin) if x["status"] in ("superseded","deployed")]; print(h[-2]["revision"] if len(h)>1 else "")')
[ -n "$previous" ] || { echo "нет предыдущей ревизии для отката"; exit 1; }
echo "rollback $release/$ns: revision $current → $previous"
t0=$(date +%s)
helm rollback "$release" "$previous" -n "$ns" --wait --timeout 10m --cleanup-on-fail
kubectl -n "$ns" rollout status deploy -l app.kubernetes.io/instance="$release" --timeout=10m
echo "rolled back in $(( $(date +%s) - t0 ))s"
if [ -n "${SMOKE_BASE_URL:-}" ]; then backend/.venv/bin/python backend/scripts/smoke_staging.py; fi
if [ "$mode" = "--drill" ]; then
  echo "drill: возвращаемся на ревизию $current"
  helm rollback "$release" "$current" -n "$ns" --wait --timeout 10m
  kubectl -n "$ns" rollout status deploy -l app.kubernetes.io/instance="$release" --timeout=10m
  [ -z "${SMOKE_BASE_URL:-}" ] || backend/.venv/bin/python backend/scripts/smoke_staging.py
fi
