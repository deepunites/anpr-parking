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
}

load_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$HERE/parking.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Создан $ENV_FILE со значениями по умолчанию (MediaPark, 54 места, подсеть 192.168.13.0/24)."
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  PARKING_HTTP_PORT=${PARKING_HTTP_PORT:-8080}
  # адрес сервера в сети площадки: по нему заходят телефоны кассиров и шлют события камеры
  LAN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n1)
  if [ -n "${LAN_IP:-}" ]; then
    export PARKING_LAN_URL="http://$LAN_IP:$PARKING_HTTP_PORT"
  fi
}

compose() {
  docker compose -p anpr-parking --env-file "$ENV_FILE" \
    -f "$ROOT/deploy/local/compose.yml" -f "$HERE/compose.override.yml" "$@"
}
