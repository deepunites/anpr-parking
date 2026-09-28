#!/usr/bin/env bash
# Еженедельная проверка бэкапа (§4): «бэкап существует, только если восстановился». systemd timer на verify-ВМ (изолирована от
# кластера, свой репозиторий-клиент pgBackRest в режиме read-only). Шаги: restore последнего набора → запуск на порту 5499 →
# archive recovery до конца WAL → pg_amcheck → контрольные суммы бизнес-данных (сессии за день, сумма платежей за день)
# сравниваются с primary → метрика backup_restore_verified_timestamp (textfile collector node_exporter; алерт
# BackupRestoreVerifiedStale > 8 дней). Лабораторный аналог с теми же проверками: deploy/postgres/lab/pgha_lab.py verify.
set -euo pipefail
STANZA="${STANZA:?pgbackrest stanza}"
PRIMARY_DSN="${PRIMARY_DSN:?postgresql://prometheus_ro@haproxy:5001/parking}"   # реплика: чтение контрольных сумм
RESTORE_DIR="${RESTORE_DIR:-/var/lib/postgresql/verify}"
PORT=5499
TEXTFILE="${TEXTFILE:-/var/lib/node_exporter/textfile_collector/backup_restore_verified.prom}"
PGBIN=/usr/lib/postgresql/16/bin
start=$(date +%s)

rm -rf "$RESTORE_DIR" && mkdir -p "$RESTORE_DIR" && chmod 700 "$RESTORE_DIR"
pgbackrest --stanza="$STANZA" --pg1-path="$RESTORE_DIR" --type=default --target-action=promote restore
cat >> "$RESTORE_DIR/postgresql.auto.conf" <<CONF
port = $PORT
listen_addresses = '127.0.0.1'
archive_mode = off
primary_conninfo = ''
primary_slot_name = ''
CONF
rm -f "$RESTORE_DIR/standby.signal"
"$PGBIN/pg_ctl" -D "$RESTORE_DIR" -w -t 900 -l "$RESTORE_DIR/restore.log" start
for _ in $(seq 1 900); do
  psql -h 127.0.0.1 -p $PORT -U postgres -d postgres -Atc "SELECT pg_is_in_recovery()" 2>/dev/null | grep -q f && break
  sleep 1
done

"$PGBIN/pg_amcheck" -h 127.0.0.1 -p $PORT -U postgres -d parking --install-missing --heapallindexed --parent-check
checks="SELECT (SELECT count(*) FROM parking_session WHERE created_at::date = current_date - 1) || ',' ||
               (SELECT coalesce(sum(amount),0) FROM payment WHERE status='PAID' AND paid_at::date = current_date - 1) || ',' ||
               (SELECT count(*) FROM outbox WHERE created_at::date = current_date - 1)"
restored=$(psql -h 127.0.0.1 -p $PORT -U postgres -d parking -Atc "$checks")
source=$(psql "$PRIMARY_DSN" -Atc "$checks")
"$PGBIN/pg_ctl" -D "$RESTORE_DIR" -m fast stop
rto=$(( $(date +%s) - start ))
if [ "$restored" = "$source" ]; then
  printf '# HELP backup_restore_verified_timestamp Unix time последней успешной проверки восстановления\n# TYPE backup_restore_verified_timestamp gauge\nbackup_restore_verified_timestamp{stanza="%s"} %s\nbackup_restore_duration_seconds{stanza="%s"} %s\n' \
    "$STANZA" "$(date +%s)" "$STANZA" "$rto" > "$TEXTFILE.tmp" && mv "$TEXTFILE.tmp" "$TEXTFILE"
  echo "backup verified: checksums $restored match; restore took ${rto}s"
else
  echo "backup verification FAILED: restored=$restored source=$source" >&2
  exit 1
fi
