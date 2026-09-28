"""I-14: приложение не стартует без обязательного конфига; prod запрещает DEBUG, '*' в CORS и ключи не из Vault."""

from __future__ import annotations

import os

import pytest

from parking.platform.config import DevSettings, Environment, load_settings

PROD_BASE = {
    "PARKING_ENV": "prod",
    "PARKING_DATABASE_URL_CRITICAL": "postgresql+asyncpg://app_critical:pw@pgbouncer:6432/parking",
    "PARKING_DATABASE_URL_BULK": "postgresql+asyncpg://app_bulk:pw@pgbouncer:6433/parking",
    "PARKING_DATABASE_URL_MAINTENANCE": "postgresql+asyncpg://app_maintenance:pw@pgbouncer:6432/parking",
    "PARKING_REDIS_URL": "redis://redis:6379/0",
    "PARKING_JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY-----",
    "PARKING_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----x-----END PUBLIC KEY-----",
    "PARKING_KEYS_SOURCE": "vault",
    "PARKING_CAMERA_KEY_PEPPER": "0123456789abcdef0123456789abcdef",
    "PARKING_VAULT_ADDR": "https://vault.internal:8200",
    "PARKING_VAULT_ROLE_ID": "role",
    "PARKING_VAULT_SECRET_ID": "secret",
    "PARKING_CORS_ORIGINS": "https://ops.parking.uz",
    "PARKING_S3_ACCESS_KEY": "s3-access",
    "PARKING_S3_SECRET_KEY": "s3-secret",
}


def _with_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(os, "environ", dict(env))


def test_empty_environment_exits_with_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_env(monkeypatch, {})
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1
    assert "PARKING_ENV" in capsys.readouterr().err


def test_prod_without_secrets_lists_missing_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_env(monkeypatch, {"PARKING_ENV": "prod"})
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "PARKING_DATABASE_URL_CRITICAL" in err and "PARKING_JWT_PRIVATE_KEY" in err and "PARKING_KEYS_SOURCE" in err


def test_prod_valid_configuration_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_env(monkeypatch, PROD_BASE)
    settings = load_settings()
    assert settings.env is Environment.PROD and settings.debug is False
    assert settings.cors_origins == ["https://ops.parking.uz"]
    assert "pw" not in repr(settings)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"PARKING_DEBUG": "true"}, "PARKING_DEBUG"),
        ({"PARKING_CORS_ORIGINS": "*"}, "CORS"),
        ({"PARKING_CORS_ORIGINS": "http://ops.parking.uz"}, "https://"),
        ({"PARKING_KEYS_SOURCE": "env"}, "vault"),
        ({"PARKING_CAMERA_KEY_PEPPER": "  "}, "PEPPER"),
        ({"PARKING_DATABASE_URL_CRITICAL": "postgresql+asyncpg://app_critical@localhost/parking"}, "localhost"),
    ],
)
def test_prod_rejects_dangerous_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], override: dict[str, str], fragment: str
) -> None:
    _with_env(monkeypatch, {**PROD_BASE, **override})
    with pytest.raises(SystemExit):
        load_settings()
    assert fragment in capsys.readouterr().err


def test_dev_has_defaults_and_ephemeral_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_env(monkeypatch, {"PARKING_ENV": "dev"})
    settings = load_settings()
    assert isinstance(settings, DevSettings)
    assert settings.jwt_private_key.get_secret_value().startswith("-----BEGIN PRIVATE KEY-----")
    assert settings.jwt_public_key.startswith("-----BEGIN PUBLIC KEY-----")
    assert len(settings.camera_key_pepper.get_secret_value()) == 64
    assert "parking_dev" in settings.database_url_critical.get_secret_value()


def test_unknown_env_exits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _with_env(monkeypatch, {"PARKING_ENV": "production"})
    with pytest.raises(SystemExit):
        load_settings()
    assert "dev|staging|prod" in capsys.readouterr().err
