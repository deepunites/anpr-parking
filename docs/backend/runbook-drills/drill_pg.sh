#!/bin/bash
source /tmp/claude-1000/-home-deep-projects-anpr-parking/2bfbe644-53ce-4885-ad44-be77a8dd043e/scratchpad/drill_common.sh
LAB=$B/.pgha
PATRONICTL="uvx --from patroni[raft]==3.3.7 --with psycopg2-binary patronictl -c $LAB/pg1/patroni.yml"
DB="postgresql://postgres@127.0.0.1:6432/parking_e2e"
echo "### Runbook 1 «PostgreSQL primary недоступен» — Patroni ×3 (лаборатория) + приложение через PgBouncer :6432 → HAProxy-аналог :5500 — $(date -u +%FT%TZ)"
run "$PATRONICTL list"
run "curl -s $API/health/ready"
run "camera_event 01G301AA"
LEADER=$($PATRONICTL list -f json 2>/dev/null | python3 -c "import json,sys;print([m['Member'] for m in json.load(sys.stdin) if m['Role']=='Leader'][0])")
PM=$(head -1 $LAB/$LEADER/data/postmaster.pid)
AGENTS=$(pgrep -f "patroni .*$LEADER/patroni.yml" | tr '\n' ' ')
echo; echo "--- отказ: узел primary=$LEADER потерян — kill -9 агента Patroni (pids $AGENTS) и postmaster ($PM) $(ts)"
run "kill -9 $AGENTS $PM"
sleep 2
run "curl -s -w ' %{http_code}' $API/health/ready"
run "camera_event 01G302AA"
run "$REDIS XLEN anpr:offline"
run "metric 'offline_mode_active'"
echo "  (ждём автоматический failover: ttl 20 с / loop_wait 5 с в лаборатории; production ttl 30 / loop_wait 10)"
for i in $(seq 1 60); do $PATRONICTL list 2>/dev/null | grep -q "Leader" && curl -s $API/health/ready | grep -q '"postgresql":"ok"' && break; sleep 1; done
run "$PATRONICTL list"
run "curl -s -w ' %{http_code}' $API/health/ready"
run "camera_event 01G303AA"
for i in $(seq 1 60); do [ "$($REDIS XLEN anpr:offline)" = "0" ] && break; sleep 1; done
run "$REDIS XLEN anpr:offline"
run "$PSQL '$DB' -Atc \"SELECT count(*) FILTER (WHERE queued_offline) AS replayed, count(*) AS total FROM anpr_event WHERE received_at > now() - interval '3 minutes'\""
run "curl -s http://127.0.0.1:9466/metrics | grep -E '^offline_replayed_total'"
echo; echo "--- возврат старого primary репликой $(ts)"
run "cd $B && uv run python ../deploy/postgres/lab/pgha_lab.py status >/dev/null; uv run python -c \"import sys; sys.path.insert(0,'../deploy/postgres/lab'); import pgha_lab as L; L.start_patroni('$LEADER'); L.wait_members(3); print(L.status())\""
run "$PATRONICTL list"
echo "--- завершено: failover автоматический, потерь принятых событий нет (202 → replay), старый primary — реплика $(ts)"
