#!/bin/sh
# Запись камер: по строке «папка rtsp-адрес id-камеры» из /config/streams.conf (пишет deploy/truenas/video.sh) ffmpeg забирает
# поток и режет его на файлы /video/<папка>/<ГГГГ-ММ-ДД>/<ЧЧ-ММ-СС>.mp4 по VIDEO_SEGMENT_SECONDS. Видео копируется как
# есть — сервер почти не нагружен. MP4 фрагментированный: файл смотрится, даже если запись оборвалась (выключили свет).
# Поток оборвался — переподключение через 10 с. Каждые 5 минут удаляются записи старше VIDEO_KEEP_DAYS дней и самые
# старые часы, пока раздел заполнен больше VIDEO_MAX_USE_PERCENT %. Пароль из адреса в журнал не попадает.
set -u
CONF=/config/streams.conf
SEGMENT=${VIDEO_SEGMENT_SECONDS:-600}
KEEP_DAYS=${VIDEO_KEEP_DAYS:-30}
MAX_USE=${VIDEO_MAX_USE_PERCENT:-90}

log() { echo "$(date '+%F %T') $*"; }

# ffmpeg пишет адрес потока в ошибках — вместе с логином и паролем
redact() { awk '{ gsub(/\/\/[^\/@ \t]+@/, "//***@"); print; fflush() }'; }

dates() {
  today=$(date +%F)
  tomorrow=$(date -d "@$(($(date +%s) + 86400))" +%F)
}

# папки на сегодня и завтра: ffmpeg сам каталоги не создаёт, а в полночь переходит в новый день
day_dirs() {
  dates
  for folder in $FOLDERS; do
    mkdir -p "/video/$folder/$today" "/video/$folder/$tomorrow"
  done
}

used_percent() { df -P "$1" | awk 'NR == 2 { sub("%", "", $5); print $5 }'; }

cleanup() {
  dates
  for folder in $FOLDERS; do
    dir=/video/$folder
    find "$dir" -mindepth 2 -maxdepth 2 -name '*.mp4' -mtime +"$KEEP_DAYS" -delete
    while [ "$(used_percent "$dir")" -gt "$MAX_USE" ]; do
      oldest=$(find "$dir" -mindepth 2 -maxdepth 2 -name '*.mp4' | sort | head -n 6)  # час записи
      [ -n "$oldest" ] || break
      echo "$oldest" | xargs rm -f
      log "$folder: раздел заполнен больше $MAX_USE % — удалены старые записи до $(echo "$oldest" | tail -n 1)"
    done
    find "$dir" -mindepth 1 -maxdepth 1 -type d -empty ! -name "$today" ! -name "$tomorrow" -delete 2>/dev/null
  done
}

record() {
  folder=$1
  url=$2
  while :; do
    log "$folder: запись"
    ffmpeg -hide_banner -nostdin -loglevel error \
      -rtsp_transport tcp -timeout 15000000 -i "$url" \
      -map 0:v:0 -c copy \
      -f segment -segment_time "$SEGMENT" -segment_atclocktime 1 -reset_timestamps 1 -strftime 1 \
      -segment_format mp4 -segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof \
      "/video/$folder/%Y-%m-%d/%H-%M-%S.mp4" 2>&1 | redact
    log "$folder: поток прервался — переподключение через 10 с"
    sleep 10
  done
}

FOLDERS=$(awk 'NF >= 2 { print $1 }' "$CONF" 2>/dev/null)
if [ -z "$FOLDERS" ]; then
  log "камер для записи нет — включите запись: sudo bash deploy/truenas/video.sh"
  while :; do sleep 3600; done
fi

trap 'trap - TERM INT; kill 0' TERM INT  # остановка контейнера: ffmpeg дописывает текущий файл
day_dirs
while read -r folder url _; do
  [ -n "${url:-}" ] && record "$folder" "$url" &
done < "$CONF"

while :; do
  sleep 300 &
  wait $!
  cleanup
  day_dirs
done
