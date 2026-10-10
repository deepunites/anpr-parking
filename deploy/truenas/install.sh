#!/usr/bin/env bash
# Установка и обновление ANPR Parking на сервере TrueNAS. Запуск из оболочки TrueNAS (System → Shell или SSH):
#   sudo bash /mnt/tank/anpr/anpr-parking/deploy/truenas/install.sh
# Первый раз: сборка образов (5–15 минут, нужен интернет), база, миграции, компания и площадка, ключ камеры.
# Повторный запуск — обновление: пересборка, новые миграции; данные, пароли и ключи сохраняются.
source "$(dirname "$0")/common.sh"
require_root
require_docker
load_env

echo "============================================================"
echo "  ANPR Parking — установка на сервер ($ROOT)"
echo "============================================================"

echo "Сборка образов — в первый раз 5–15 минут..."
compose build

echo "Запуск сервисов..."
compose up -d --remove-orphans || {
  echo "Сервисы не запустились. Журнал первичной настройки:"
  compose logs --tail=80 init
  fail "Запуск не удался — пришлите этот журнал."
}

echo "Ожидание готовности http://127.0.0.1:$PARKING_HTTP_PORT ..."
for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PARKING_HTTP_PORT/api/v1/plans" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [ -z "${ready:-}" ]; then
  compose logs --tail=60 api
  fail "Веб не ответил за 3 минуты — пришлите журнал выше."
fi

(umask 077 && compose exec -T api cat /state/credentials.txt > "$HERE/LOGINS.txt")  # пароли — сразу 600
cat "$HERE/LOGINS.txt"
echo "============================================================"
echo "  Готово."
if [ -n "${PARKING_LAN_URL:-}" ]; then
  echo "  Панель (компьютер, телефоны кассиров, камеры): $PARKING_LAN_URL"
else
  echo "  [!] Не найден адрес сервера в сети — проверьте Network → Interfaces в TrueNAS."
fi
echo "  Логин и пароль администратора, ключ камеры: $HERE/LOGINS.txt"
echo "  Сервисы перезапускаются сами после перезагрузки сервера."
echo "  Журналы: sudo bash $HERE/logs.sh    Остановить: sudo bash $HERE/stop.sh"
echo "============================================================"
