"""Обёртка процессов приложения на стенде: окружение PARKING_* из секретов тома /state, затем exec команды сервиса.

Значения стенда главнее окружения образа (в Dockerfile бэкенда PARKING_ENV=prod — для кластера с Vault); прочие
PARKING_* из compose проходят как есть — например, PARKING_SIGNUP_ENABLED=false выключает регистрацию.
"""

from __future__ import annotations

import os
import sys

from standlib import load_secrets, stand_env


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: entry.py <command> [args...]")
    env = {**os.environ, **stand_env(load_secrets())}
    os.execvpe(sys.argv[1], sys.argv[1:], env)  # noqa: S606 — команда сервиса из compose, не пользовательский ввод


if __name__ == "__main__":
    main()
