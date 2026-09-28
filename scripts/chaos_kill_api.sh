#!/usr/bin/env bash
# «Убит под API → трафик не потерян» (gate фазы 9): k6 baseline (÷10) против staging, посреди прогона kubectl delete pod
# одного из подов API; критерий — events_failed == 0 и http_req_failed{camera_events} == 0 (PDB minAvailable, readiness
# gate и preStop sleep гарантируют, что endpoint убирается до SIGTERM; uvicorn дорабатывает открытые запросы).
#   scripts/chaos_kill_api.sh <namespace> <base_url>
set -euo pipefail
ns="${1:?namespace}"; base="${2:?base url}"
# NetworkPolicy (gate): под фронтенда не достучался до PostgreSQL/Redis — соединение должно упасть по таймауту
front=$(kubectl -n "$ns" get pods -l app.kubernetes.io/component=frontend -o jsonpath='{.items[0].metadata.name}')
pg_host=$(kubectl -n "$ns" get networkpolicy -o jsonpath='{.items[?(@.metadata.name=="parking-parking-api")].spec.egress[0].to[0].ipBlock.cidr}' | cut -d/ -f1)
if kubectl -n "$ns" exec "$front" -- timeout 3 sh -c "exec 3<>/dev/tcp/${pg_host%.*}.11/6432" 2>/dev/null; then
  echo "GATE networkpolicy: FAIL — фронтенд достучался до PostgreSQL"; exit 1
else
  echo "GATE networkpolicy: frontend → PostgreSQL:6432 заблокирован (default deny)"
fi
pods=($(kubectl -n "$ns" get pods -l app.kubernetes.io/component=api -o jsonpath='{.items[*].metadata.name}'))
[ "${#pods[@]}" -ge 2 ] || { echo "нужно ≥ 2 реплики API"; exit 1; }
K6_SCENARIO=baseline SCALE=60 RATE_SCALE=10 API="$base" k6 run --quiet --summary-export /tmp/chaos.json load/k6/parking.js &
k6pid=$!
sleep 20
victim="${pods[0]}"
echo "$(date -u +%T) delete pod $victim during k6"
kubectl -n "$ns" delete pod "$victim" --wait=false
wait $k6pid || true
python3 - <<'PY'
import json
m = json.load(open("/tmp/chaos.json"))["metrics"]
failed = m.get("events_failed", {}).get("count", 0)
rate = m.get("http_req_failed{name:camera_events}", {}).get("value", 0)
reqs = m.get("http_reqs", {}).get("count", 0)
print(f"GATE kill api pod: requests={reqs} events_failed={failed} http_req_failed(camera)={rate:.5f}")
raise SystemExit(0 if failed == 0 and rate == 0 else 1)
PY
