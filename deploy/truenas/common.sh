# Общее для скриптов сервера TrueNAS (подключается через source): проверки, окружение, команда docker compose.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
HERE="$ROOT/deploy/truenas"
ENV_FILE="$HERE/parking.env"
cd "$ROOT"

fail() {
  echo
  echo "[!] $*" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "Нужны права администратора: sudo bash $0"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || fail "Не найден Docker. В TrueNAS: Apps → Configuration → Choose Pool (выберите пул)."
  docker info >/dev/null 2>&1 || fail "Docker не запущен. В TrueNAS: Apps → Configuration → Choose Pool и дождитесь «Running»."
  docker compose version >/dev/null 2>&1 || fail "Нет docker compose — нужна TrueNAS Community Edition 25.04 или новее."
  # Dockerfile'ы используют BuildKit (RUN --mount): без buildx сборка падает с непонятной ошибкой
  docker buildx version >/dev/null 2>&1 || echo "[!] Нет docker buildx — если сборка упадёт, пришлите журнал: нужен плагин buildx."
}

load_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$HERE/parking.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Создан $ENV_FILE со значениями по умолчанию (MediaPark, 54 места, подсеть 192.168.13.0/24)."
  fi
  # файл не исполняется как скрипт (значения с пробелами, $ и кавычками — законны для docker compose --env-file):
  # скриптам нужны только порт и резервные копии, остальное читает compose
  PARKING_HTTP_PORT=$(env_value PARKING_HTTP_PORT 8080)
  # shellcheck disable=SC2034  # используются в backup.sh
  PARKING_BACKUP_DIR=$(env_value PARKING_BACKUP_DIR /mnt/tank/anpr/backup)
  # shellcheck disable=SC2034
  PARKING_BACKUP_KEEP_DAYS=$(env_value PARKING_BACKUP_KEEP_DAYS 14)
  # адрес сервера в сети площадки: по нему заходят телефоны кассиров и шлют события камеры (без шлюза — пусто)
  LAN_IP=$( (ip -4 route get 1.1.1.1 2>/dev/null || true) | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n1)
  if [ -n "${LAN_IP:-}" ]; then
    export PARKING_LAN_URL="http://$LAN_IP:$PARKING_HTTP_PORT"
  fi
}

# значение KEY из parking.env (последнее вхождение, без кавычек вокруг) или значение по умолчанию
env_value() {
  local value
  value=$(sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
  echo "${value:-$2}"
}

VIDEO_STREAMS="$HERE/video-streams.conf"  # список потоков камер (video.sh); есть — запись включена

compose() {
  local files=(-f "$ROOT/deploy/local/compose.yml" -f "$HERE/compose.override.yml")
  if [ -s "$VIDEO_STREAMS" ]; then
    files+=(-f "$HERE/compose.video.yml")  # сервис записи и доступ API к ней
  fi
  docker compose -p anpr-parking --env-file "$ENV_FILE" "${files[@]}" "$@"
}

# nginx (сервис web) находит api по имени один раз при старте: пересозданный api получает новый адрес, и до
# перезапуска web панель отвечает 502 («Веб не ответил»). После любого обновления api — перезапуск web.
restart_web() {
  compose restart web >/dev/null
}
