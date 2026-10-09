#!/usr/bin/env bash
# Резервная копия: база (pg_dump) и том state — ключи шифрования номеров, пароли БД, секреты. Без state зашифрованные
# номера из копии базы не прочитать, поэтому они сохраняются вместе. Копии старше PARKING_BACKUP_KEEP_DAYS удаляются.
# Ежедневно: TrueNAS → System → Advanced Settings → Cron Jobs → команда «bash <этот файл>», пользователь root.
source "$(dirname "$0")/common.sh"
require_root
require_docker
load_env
DIR=${PARKING_BACKUP_DIR:?укажите PARKING_BACKUP_DIR в parking.env}
KEEP=${PARKING_BACKUP_KEEP_DAYS:-14}
STAMP=$(date +%Y-%m-%d_%H%M)
mkdir -p "$DIR"
chmod 700 "$DIR"
umask 077

compose exec -T postgres sh -c 'PGPASSWORD="$(cat /state/pg_password)" pg_dump -h 127.0.0.1 -U postgres -Fc parking' \
  > "$DIR/parking-$STAMP.dump.part"
mv "$DIR/parking-$STAMP.dump.part" "$DIR/parking-$STAMP.dump"
compose exec -T api tar -czf - -C /state . > "$DIR/state-$STAMP.tar.gz.part"
mv "$DIR/state-$STAMP.tar.gz.part" "$DIR/state-$STAMP.tar.gz"

find "$DIR" -maxdepth 1 -type f \( -name 'parking-*.dump' -o -name 'state-*.tar.gz' \) -mtime +"$KEEP" -delete
echo "Резервная копия: $DIR/parking-$STAMP.dump, $DIR/state-$STAMP.tar.gz"
