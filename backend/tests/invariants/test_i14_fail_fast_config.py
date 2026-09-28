"""I-14. Приложение не стартует, если отсутствует обязательный секрет или конфиг.

Механизм: pydantic-settings с раздельными классами Dev/Staging/Prod; load_settings() валидирует на старте и делает
sys.exit(1) с перечнем недостающих переменных; prod отвергает опасные значения (ephemeral-ключи, keys_source=env,
DEBUG, wildcard CORS); create_app вызывает load_settings до регистрации роутов.
При нарушении: API поднимается с пустым JWT-ключом/без шифрования номеров и принимает трафик — тихая катастрофа.
"""

from __future__ import annotations

import os

import pytest

from parking.platform.config import load_settings
from tests.platform.test_config import PROD_BASE


def _env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(os, "environ", dict(env))


@pytest.mark.parametrize(
    "missing",
    [
        "PARKING_DATABASE_URL_CRITICAL",
        "PARKING_DATABASE_URL_BULK",
        "PARKING_DATABASE_URL_MAINTENANCE",
        "PARKING_REDIS_URL",
        "PARKING_JWT_PRIVATE_KEY",
        "PARKING_KEYS_SOURCE",
    ],
)
def test_prod_exits_when_required_setting_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    env = {k: v for k, v in PROD_BASE.items() if k != missing}
    _env(monkeypatch, env)
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1
    assert missing in capsys.readouterr().err, "сообщение называет недостающую переменную"


@pytest.mark.parametrize(
    ("key", "value"),
    [("PARKING_KEYS_SOURCE", "env"), ("PARKING_DEBUG", "true"), ("PARKING_CORS_ORIGINS", '["*"]')],
)
def test_prod_exits_on_dangerous_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str, value: str
) -> None:
    _env(monkeypatch, {**PROD_BASE, key: value})
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1 and capsys.readouterr().err


def test_no_environment_at_all_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, {})
    with pytest.raises(SystemExit):
        load_settings()


def test_create_app_refuses_to_build_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from parking.api.app import create_app

    _env(monkeypatch, {"PARKING_ENV": "prod"})
    with pytest.raises(SystemExit):
        create_app(load_settings())
