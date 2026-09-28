#!/bin/bash
source /tmp/claude-1000/-home-deep-projects-anpr-parking/2bfbe644-53ce-4885-ad44-be77a8dd043e/scratchpad/drill_common.sh
DB="postgresql://postgres@/parking_e2e?host=$B/.pgdata"
PLATFORM_TOKEN=$(token e2e.platform@alpha-parking.uz $(python3 -c "import json;print(json.load(open('$STATE'))['users']['platform_admin']['password'])"))
echo "### Runbook 7 «Накопление DLQ» (после отказа PostgreSQL) — стенд e2e_stack — $(date -u +%FT%TZ)"
run "camera_event 01D201AA"
run "$REDIS XLEN events:dlq"
# событие, которое consumer получит во время отказа БД: копия реального outbox-события (session.created)
EV=$($PSQL "$DB" -Atc "SELECT event_id||'|'||tenant_id||'|'||aggregate_id||'|'||event_type||'|'||correlation_id||'|'||payload::text FROM outbox WHERE event_type='parking.session.created.v1' ORDER BY created_at DESC LIMIT 1")
IFS='|' read -r EID TID AID ETYPE CID PAYLOAD <<< "$EV"
echo; echo "--- отказ: остановка PostgreSQL $(ts) (события камер уходят в оффлайн-буфер, обработчики consumer'а падают)"
run "uv run python $B/scripts/pg_dev.py stop"
run "camera_event 01D202AA"
run "$REDIS XLEN anpr:offline"
NEWID=$(python3 -c "import uuid;print(uuid.uuid4())")
run "$REDIS XADD events:1 '*' event_id $NEWID event_type $ETYPE tenant_id $TID aggregate_type parking_session aggregate_id $AID occurred_at $(date -u +%Y-%m-%dT%H:%M:%S+00:00) correlation_id $CID trace_parent '' payload '$PAYLOAD' attempt 0"
echo "  (ждём 3 попытки обработчика: 1 с + 4 с + 16 с бэкоффа)"
for i in $(seq 1 90); do n=$($REDIS XLEN events:dlq); [ "$n" != "0" ] && break; sleep 1; done
run "$REDIS XLEN events:dlq"
run "$REDIS XREVRANGE events:dlq + - COUNT 1 | grep -A1 -E 'error|event_type' | head -4"
run "metric 'dlq_length\|dlq_messages_total' 4"
run "curl -s -H \"Authorization: Bearer $PLATFORM_TOKEN\" '$API/api/v1/admin/dlq?stream=events&limit=5' | python3 -c \"import json,sys;d=json.load(sys.stdin);[print(e['id'], e['fields'].get('event_type'), '|', e['fields'].get('error','')[:90]) for e in (d if isinstance(d, list) else d.get('items', []))]\""
echo; echo "--- восстановление: запуск PostgreSQL $(ts)"
run "uv run python $B/scripts/pg_dev.py start"
for i in $(seq 1 30); do curl -s $API/health/ready | grep -q '"postgresql":"ok"' && break; sleep 1; done
run "curl -s -w ' %{http_code}' $API/health/ready"
for i in $(seq 1 30); do [ "$($REDIS XLEN anpr:offline)" = "0" ] && break; sleep 1; done
run "$REDIS XLEN anpr:offline"
run "metric 'offline_replayed_total' 4"
echo; echo "--- replay DLQ: сначала dry-run, затем боевой (PLATFORM_ADMIN, reason обязателен)"
run "curl -s -X POST -H \"Authorization: Bearer $PLATFORM_TOKEN\" -H 'Content-Type: application/json' '$API/api/v1/admin/dlq/replay' -d '{\"stream\":\"events\",\"dry_run\":true,\"reason\":\"runbook 7: после восстановления БД\"}' | python3 -c \"import json,sys;d=json.load(sys.stdin);print('dry_run', d['dry_run'], 'count', d['count'])\""
run "curl -s -X POST -H \"Authorization: Bearer $PLATFORM_TOKEN\" -H 'Content-Type: application/json' '$API/api/v1/admin/dlq/replay' -d '{\"stream\":\"events\",\"dry_run\":false,\"reason\":\"runbook 7: после восстановления БД\"}' | python3 -c \"import json,sys;d=json.load(sys.stdin);print('dry_run', d['dry_run'], 'count', d['count'])\""
sleep 4
run "$REDIS XLEN events:dlq"
run "$PSQL '$DB' -Atc \"SELECT count(*) FROM consumer_inbox WHERE event_id = '$NEWID'\""
run "$PSQL '$DB' -Atc \"SELECT action, created_at FROM audit_log WHERE action LIKE 'DLQ%' ORDER BY created_at DESC LIMIT 2\""
echo "--- DLQ пуст, событие обработано один раз (inbox), replay записан в audit_log $(ts)"
