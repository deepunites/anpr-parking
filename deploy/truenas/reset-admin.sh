#!/usr/bin/env bash
# Забыт пароль администратора (или другого сотрудника): новый временный пароль, при входе система попросит свой.
#   sudo bash /mnt/tank/anpr/anpr-parking/deploy/truenas/reset-admin.sh              — администратор из parking.env
#   sudo bash /mnt/tank/anpr/anpr-parking/deploy/truenas/reset-admin.sh +998901234567 — сотрудник по логину
# Пароль печатается только здесь, на экране консоли; старые сессии сотрудника завершаются, сброс — в журнале аудита.
source "$(dirname "$0")/common.sh"
require_root
require_docker
load_env

LOGIN=${1:-$(env_value PARKING_ADMIN_LOGIN admin@mediapark.uz)}
compose exec -T api python /stand/entry.py python /stand/reset_password.py "$LOGIN"
