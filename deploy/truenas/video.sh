#!/usr/bin/env bash
# Круглосуточная запись камер на диск сервера. Камера DS-TCG406-E сама на сетевой диск не пишет (в прошивке нет NAS),
# поэтому поток забирает сервер: сервис video (deploy/truenas/video) режет его на 10-минутные файлы
#   <PARKING_VIDEO_DIR>/cam-entry/2026-10-12/15-40-00.mp4   (cam-exit — камера выезда)
# Адрес, логин и пароль — «Видеозапись» камеры в «Устройствах» панели, а если там пусто — логин и пароль её стрелы.
# Ролики въезда и выезда в карточке сессии панель берёт из этой же записи.
#   sudo bash video.sh          — включить запись или обновить список камер (после новой камеры или пароля)
#   sudo bash video.sh status   — что пишется и сколько места
#   sudo bash video.sh off      — выключить запись (записанные файлы остаются)
source "$(dirname "$0")/common.sh"
require_root
require_docker
load_env
VIDEO_DIR=$(env_value PARKING_VIDEO_DIR /mnt/records)
STREAM=$(env_value PARKING_VIDEO_STREAM 101)
APPS_UID=568  # пользователь «apps» TrueNAS — от него работает запись

status() {
  compose ps video
  local folder dir last age
  while read -r folder _; do
    dir="$VIDEO_DIR/$folder"
    last=$(find "$dir" -mindepth 2 -maxdepth 2 -name '*.mp4' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n1 | cut -d' ' -f2-)
    echo
    echo "  $folder: занято $(du -sh "$dir" | cut -f1) из $(df -h --output=size "$dir" | tail -n1 | tr -d ' ')"
    if [ -n "$last" ]; then
      age=$(($(date +%s) - $(stat -c %Y "$last")))
      echo "    последний файл ${last#"$dir"/}: $(du -h "$last" | cut -f1), обновлён $age с назад"
    else
      echo "    файлов пока нет"
    fi
  done < "$VIDEO_STREAMS"
  echo
  echo "  Журнал записи: sudo bash $HERE/logs.sh video"
}

case "${1:-on}" in
  status)
    [ -s "$VIDEO_STREAMS" ] || fail "Запись выключена. Включить: sudo bash $0"
    status
    exit 0
    ;;
  off)
    if [ -s "$VIDEO_STREAMS" ]; then
      compose rm -sf video
      rm -f "$VIDEO_STREAMS"
      compose up -d api  # без тома с записью
    fi
    echo "Запись камер выключена, файлы остались в $VIDEO_DIR."
    exit 0
    ;;
  on) ;;
  *) fail "usage: video.sh [on|status|off]" ;;
esac

[ -d "$VIDEO_DIR" ] || fail "Нет папки $VIDEO_DIR — создайте пул и разделы для видео (README, «Видеозапись камер»)."
case "$VIDEO_DIR" in
  /mnt/*/*) ;;
  /mnt/*) mountpoint -q "$VIDEO_DIR" || fail "$VIDEO_DIR — не пул TrueNAS: запись заполнила бы системный диск." ;;
esac

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
compose exec -T api python /stand/entry.py python /stand/camera_streams.py "$STREAM" > "$tmp" ||
  fail "Не удалось получить список камер — парковка запущена? (sudo bash $HERE/install.sh)"
[ -s "$tmp" ] || fail "Нет камер для записи: у камеры нужна «Видеозапись» (адрес, логин, пароль) или стрела «Камера
    Hikvision» с логином и паролем (панель → «Устройства»)."

while read -r folder _ camera; do
  mkdir -p "$VIDEO_DIR/$folder"
  echo "$camera" > "$VIDEO_DIR/$folder/.camera"  # чья папка — для роликов в карточке сессии
  chown "$APPS_UID:$APPS_UID" "$VIDEO_DIR/$folder" "$VIDEO_DIR/$folder/.camera"
  chmod 755 "$VIDEO_DIR/$folder"  # API (пользователь 10001) читает запись; сама папка — только на сервере
done < "$tmp"
if [ -s "$VIDEO_STREAMS" ] && cmp -s "$tmp" "$VIDEO_STREAMS" && [ -n "$(compose ps -q video 2>/dev/null)" ]; then
  echo "Список камер не изменился — запись идёт."
  status
  exit 0
fi
install -m 600 -o "$APPS_UID" -g "$APPS_UID" "$tmp" "$VIDEO_STREAMS"  # в файле пароли камер

echo "Сборка и запуск записи..."
compose build video
compose up -d --force-recreate video
compose up -d api  # том с записью и часовой пояс — для роликов в карточке сессии
echo "Ждём первые секунды записи..."
sleep 30
status
