#!/usr/bin/env bash
# Остановить парковку на сервере (данные сохраняются; запустить снова — install.sh).
source "$(dirname "$0")/common.sh"
require_root
require_docker
load_env
compose stop
echo "Остановлено. Запустить снова: sudo bash $HERE/install.sh"
