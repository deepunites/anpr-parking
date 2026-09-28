#!/bin/bash
source /tmp/claude-1000/-home-deep-projects-anpr-parking/2bfbe644-53ce-4885-ad44-be77a8dd043e/scratchpad/drill_common.sh
echo "### Runbook 3 «Redis недоступен» — стенд e2e_stack (API :8010, Redis :6390) — $(date -u +%FT%TZ)"
run "curl -s $API/health/ready"
run "camera_event 01R101AA"
run "$REDIS PING"
echo; echo "--- отказ: остановка Redis $(ts)"
run "$B/.redis/bin/redis-cli -p 6390 shutdown nosave"
sleep 6
run "$REDIS PING"
run "curl -s -w ' %{http_code}' $API/health/ready"
run "camera_event 01R102AA"
run "metric 'dependency_up'"
run "metric 'redis_up\|camera_event_total' 6"
echo; echo "--- восстановление: запуск Redis $(ts)"
run "uv run python $B/scripts/redis_dev.py start 6390"
for i in $(seq 1 20); do curl -s $API/health/ready | grep -q '"redis":"ok"' && break; sleep 1; done
run "curl -s -w ' %{http_code}' $API/health/ready"
run "camera_event 01R103AA"
sleep 20
run "metric 'dependency_up'"
run "grep -c worker_loop_crashed $B/.e2e/consumer.log $B/.e2e/publisher.log $B/.e2e/replayer.log"
run "curl -s http://127.0.0.1:9465/metrics | grep -E '^worker_loop_restarts_total|^consumer_processed_total.*session.created' | head -3"
run "pgrep -fc 'main_consumer|main_publisher|main_offline_replayer'"
echo "--- восстановлено без ручных действий: воркеры пережили отказ (супервизор перезапустил циклы) $(ts)"
