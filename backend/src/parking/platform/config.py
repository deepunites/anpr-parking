"""Конфигурация: pydantic-settings, валидация на старте, `sys.exit(1)` при ошибке (I-14).

Классы по окружению: dev допускает дефолты и генерирует dev-ключи; staging/prod — запрещают дефолты для секретов,
DEBUG и `*` в CORS на уровне валидаторов. `PARKING_ENV` обязателен: без него процесс не стартует.
"""

from __future__ import annotations

import enum
import os
import sys
from pathlib import Path
from typing import Annotated, Literal, Self

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[3]
ENV_VAR = "PARKING_ENV"


class Environment(enum.StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    """Общие поля. Наследники задают окружение и строгость."""

    model_config = SettingsConfigDict(env_prefix="PARKING_", extra="ignore", frozen=True)

    env: Environment
    service_name: str = "parking-api"
    version: str = "0.1.0"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url_critical: SecretStr
    database_url_bulk: SecretStr
    database_url_maintenance: SecretStr
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    redis_socket_timeout_seconds: float = Field(
        default=0.2, gt=0, le=10, description="Кеши/стримы отвечают за миллисекунды; таймаут → fail-open/буфер"
    )
    redis_url: SecretStr

    jwt_private_key: SecretStr = Field(description="PEM Ed25519 private key")
    jwt_public_key: str = Field(description="PEM Ed25519 public key")
    jwt_issuer: str = "parking"
    access_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=3600)

    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    keys_source: Literal["env", "vault"]
    tenant_keys_json: SecretStr = Field(
        default=SecretStr(""), description='{"<tenant_id>": {"hmac": b64, "dek": b64, "version": 1}}'
    )
    camera_key_pepper: SecretStr
    key_wrapping_key: SecretStr = Field(
        default=SecretStr(""),
        description="v2: KEK платформы (base64, 32 байта) для ключей клиентов, созданных при самостоятельной "
        "регистрации. Пусто — регистрация выключена (503 SIGNUP_DISABLED).",
    )
    key_refresh_seconds: float = Field(default=30.0, ge=1, le=3600, description="Период подхвата ключей новых клиентов")
    signup_enabled: bool = True
    signup_code_ttl_seconds: int = Field(default=15 * 60, ge=60, le=3600)
    signup_dev_codes: bool = Field(
        default=False, description="Код подтверждения в ответе API — только dev/test (стенд без SMS/Telegram)"
    )
    signup_rate_per_hour: int = Field(default=10, ge=1, le=1000, description="Заявок на регистрацию с одного IP в час")
    webhook_allow_private: bool = Field(
        default=False, description="v2 этап 6: вебхуки на частные/локальные адреса — только стенд и закрытый контур"
    )
    webhook_allow_http: bool = Field(default=False, description="вебхуки по http:// (без TLS) — только стенд")
    prometheus_url: str | None = Field(
        default=None, description="v2 этап 7: Prometheus для блока «здоровье» дашборда владельца платформы"
    )
    barrier_dispatch: Literal["inline", "celery"] = Field(
        default="inline",
        description="Исполнитель команд шлагбаума из consumer'а: inline (dev/тесты) или очередь Celery barrier (prod)",
    )
    barrier_drivers_enabled: bool = Field(
        default=True, description="Драйверы шлагбаумов; выключено — команды честно FAILED"
    )
    barrier_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    barrier_retries: int = Field(default=3, ge=1, le=5)
    barrier_breaker_threshold: int = Field(default=5, ge=1)
    barrier_breaker_ttl_seconds: float = Field(default=60.0, gt=0)
    secrets_json: SecretStr = Field(
        default=SecretStr(""), description='{"<secret_ref>": "<value>"} — dev/staging; prod — Vault'
    )
    otel_exporter_endpoint: str = Field(
        default="", description="OTLP/HTTP коллектор, напр. http://otel-collector:4318; пусто — без экспорта"
    )
    metrics_port: int = Field(
        default=0, ge=0, description=">0 — воркер отдаёт /metrics на этом порту (API отдаёт /metrics сам)"
    )
    otel_entry_sample_ratio: float = Field(
        default=0.02, ge=0, le=1, description="Доля успешных ENTRY-трасс (фаза 7 §3)"
    )
    otel_api_sample_ratio: float = Field(default=0.1, ge=0, le=1)
    sentry_dsn: SecretStr = Field(default=SecretStr(""), description="Пусто — Sentry выключен (dev/тесты)")
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0, le=1)
    provider_timeout_seconds: float = Field(
        default=5.0, gt=0, le=30, description="Таймаут вызова платёжного провайдера"
    )
    provider_base_urls: dict[str, str] = Field(
        default_factory=dict,
        description='Базовые URL провайдеров (тестовый контур): {"PAYME": "https://checkout.test.paycom.uz"}',
    )
    camera_rate_limit_per_minute: int = Field(
        default=120, ge=1, description="2 события/с на ключ: полоса пропускает ≤ 1 машину в 3–5 с"
    )
    camera_late_after_seconds: int = Field(
        default=600, ge=1, description="event_time старше — late: сессия задним числом, шлагбаум не трогаем"
    )
    camera_auth_abuse_threshold: int = Field(
        default=20, ge=1, description="Неудач по префиксу за 15 мин до записи в аудит"
    )
    offline_stream_maxlen: int = Field(default=1_000_000, ge=1000)
    offline_stream_alarm_ratio: float = Field(default=0.8, gt=0, lt=1)
    db_breaker_threshold: int = Field(default=3, ge=1)  # таймаут БД стоит ≥ 3–10 с: открываемся быстро (фаза 8 §5)
    db_breaker_ttl_seconds: float = Field(default=10.0, gt=0)
    reference_cache_ttl_seconds: float = Field(
        default=10.0,
        ge=0,
        le=300,
        description="Кеш процесса площадки/шлагбаума/тарифа: изменение справочника видно всем подам ≤ TTL; 0 — выкл.",
    )
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_bucket: str = "anpr-media"
    s3_region: str = "uz-tashkent-1"
    s3_presign_put_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    s3_presign_get_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    media_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    video_dir: str = Field(
        default="", description="Запись камер на сервере площадки (deploy/truenas/video.sh); пусто — видео сессий нет"
    )
    video_timezone: str = Field(default="UTC", description="Часовой пояс времени в именах файлов записи")
    video_clip_before_seconds: int = Field(default=10, ge=0, le=120, description="Ролик сессии: секунд до события")
    video_clip_after_seconds: int = Field(default=20, ge=1, le=300, description="Ролик сессии: секунд после события")
    video_clip_cache_dir: str = "/tmp/parking-video-clips"  # noqa: S108 — единственный пишущий каталог образа
    receipt_printer: str = Field(
        default="", description="Принтер чеков кассы (ESC/POS по сети): «IP:порт», например 192.168.13.240:9100"
    )
    video_link_ttl_seconds: int = Field(default=300, ge=30, le=3600, description="Сколько живёт ссылка на ролик")
    vault_addr: str | None = None
    vault_role_id: SecretStr | None = None
    vault_secret_id: SecretStr | None = None
    vault_kv_mount: str = "kv"
    vault_keys_path: str = "parking/production/tenant-keys"
    vault_namespace: str | None = None
    vault_ca_file: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value


class DevSettings(Settings):
    """Локальная разработка: дефолты указывают на pgserver в backend/.pgdata; ключи генерируются на старте."""

    model_config = SettingsConfigDict(
        env_prefix="PARKING_", extra="ignore", frozen=True, env_file=BACKEND_DIR / ".env.dev"
    )

    env: Environment = Environment.DEV
    debug: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    database_url_critical: SecretStr = SecretStr(
        f"postgresql+asyncpg://app_critical@/parking_dev?host={BACKEND_DIR / '.pgdata'}"
    )
    database_url_bulk: SecretStr = SecretStr(
        f"postgresql+asyncpg://app_bulk@/parking_dev?host={BACKEND_DIR / '.pgdata'}"
    )
    database_url_maintenance: SecretStr = SecretStr(
        f"postgresql+asyncpg://app_maintenance@/parking_dev?host={BACKEND_DIR / '.pgdata'}"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    jwt_private_key: SecretStr = SecretStr("")
    jwt_public_key: str = ""
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])
    keys_source: Literal["env", "vault"] = "env"
    camera_key_pepper: SecretStr = SecretStr("")
    webhook_allow_private: bool = True  # стенд: тестовый приёмник на локальном адресе
    webhook_allow_http: bool = True

    @model_validator(mode="after")
    def _generate_dev_secrets(self) -> Self:
        """Dev без ключей в окружении — эфемерная пара Ed25519 и pepper на время процесса (не константы в коде)."""
        if not self.jwt_private_key.get_secret_value() or not self.jwt_public_key:
            private_pem, public_pem = generate_ed25519_pem()
            object.__setattr__(self, "jwt_private_key", SecretStr(private_pem))
            object.__setattr__(self, "jwt_public_key", public_pem)
        if not self.camera_key_pepper.get_secret_value():
            object.__setattr__(self, "camera_key_pepper", SecretStr(os.urandom(32).hex()))
        return self


class HardenedSettings(Settings):
    """staging и prod: без дефолтов для секретов; DEBUG и wildcard CORS запрещены; ключи — только из Vault."""

    @model_validator(mode="after")
    def _hardened(self) -> Self:
        problems: list[str] = []
        if self.debug:
            problems.append("PARKING_DEBUG must be false")
        if any(o.strip() == "*" or not o.startswith("https://") for o in self.cors_origins):
            problems.append("PARKING_CORS_ORIGINS must list explicit https:// origins, '*' is forbidden")
        if self.keys_source != "vault":
            problems.append("PARKING_KEYS_SOURCE must be 'vault'")
        if not self.vault_addr or self.vault_role_id is None or self.vault_secret_id is None:
            problems.append("PARKING_VAULT_ADDR / PARKING_VAULT_ROLE_ID / PARKING_VAULT_SECRET_ID are required")
        if self.secrets_json.get_secret_value().strip():
            problems.append("PARKING_SECRETS_JSON is forbidden outside dev (secrets come from Vault)")
        if self.signup_dev_codes:
            problems.append("PARKING_SIGNUP_DEV_CODES is forbidden outside dev (codes go through SMS/Telegram only)")
        for name in (
            "jwt_private_key",
            "camera_key_pepper",
            "database_url_critical",
            "redis_url",
            "s3_access_key",
            "s3_secret_key",
        ):
            value: SecretStr = getattr(self, name)
            if not value.get_secret_value().strip():
                problems.append(f"PARKING_{name.upper()} must not be empty")
        if (
            "localhost" in self.database_url_critical.get_secret_value()
            or "host=/" in self.database_url_critical.get_secret_value()
        ):
            problems.append("PARKING_DATABASE_URL_CRITICAL points to a local socket/localhost")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class StagingSettings(HardenedSettings):
    env: Environment = Environment.STAGING


class ProdSettings(HardenedSettings):
    env: Environment = Environment.PROD


_CLASSES: dict[Environment, type[Settings]] = {
    Environment.DEV: DevSettings,
    Environment.STAGING: StagingSettings,
    Environment.PROD: ProdSettings,
}


def generate_ed25519_pem() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


class ConfigError(SystemExit):
    """Процесс не стартует: код 1 и понятное сообщение в stderr."""

    def __init__(self, message: str) -> None:
        print(f"CONFIG ERROR: {message}", file=sys.stderr)  # noqa: T201 — единственное место вывода до инициализации логов
        super().__init__(1)


def load_settings() -> Settings:
    """Читает окружение, выбирает класс по PARKING_ENV, валидирует. При ошибке — sys.exit(1) (I-14)."""
    raw_env = os.environ.get(ENV_VAR)
    if not raw_env:
        raise ConfigError(f"{ENV_VAR} is required (dev|staging|prod); refusing to guess")
    try:
        environment = Environment(raw_env.lower())
    except ValueError:
        raise ConfigError(f"{ENV_VAR}={raw_env!r} is not one of dev|staging|prod") from None
    cls = _CLASSES[environment]
    try:
        return cls()
    except ValidationError as exc:
        lines = [f"PARKING_{str(e['loc'][0]).upper()}: {e['msg']}" if e["loc"] else str(e["msg"]) for e in exc.errors()]
        raise ConfigError(f"invalid configuration for {environment.value}:\n  " + "\n  ".join(lines)) from None
