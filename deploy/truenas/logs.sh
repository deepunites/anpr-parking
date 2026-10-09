#!/usr/bin/env bash
# Журналы сервисов парковки. Без аргументов — последние строки всех сервисов и дальше в реальном времени (Ctrl+C —
# выход); с аргументом — один сервис: sudo bash logs.sh api   (api, init, consumer, web, postgres, minio …)
source "$(dirname "$0")/common.sh"
require_root
require_docker
load_env
compose logs --tail=100 -f "$@"
