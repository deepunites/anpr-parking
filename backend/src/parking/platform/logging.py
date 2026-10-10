"""structlog в JSON с редактором ПДн и секретов (I-10).

Редактор — последний процессор перед рендерером: вырезает по ключам (password, token, secret, plate…) и по значениям
(JWT, номера ТС, длинные ключи). Тест: `tests/platform/test_logging_redaction.py`.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import orjson
import structlog

from parking.platform.tracing import current_trace_ids

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|jwt|authorization|cookie|api[_-]?key|x-camera-key|pepper|private[_-]?key|"
    r"plate(?!_hash|_format|_encrypted)|plate_number|raw_plate|(?<![a-z])(card|pan|cvv)(?![a-z]))",
    re.IGNORECASE,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
# номера: узбекские форматы с разделителями и без, латиница/кириллица
_PLATE = re.compile(
    r"(?<![A-Z0-9])\d{2}\s?[A-ZА-Я]\s?\d{3}\s?[A-ZА-Я]{2}(?![A-Z0-9])|(?<![A-Z0-9])\d{5}\s?[A-ZА-Я]{3}(?![A-Z0-9])"
)
# ключ камеры Hikvision/Dahua — последний сегмент пути (камеры не умеют свой заголовок): маска для всех журналов
# (алфавит ключа — token_urlsafe: шаблон роута «{camera_key}» в метках метрик не трогается)
CAMERA_KEY_PATH = re.compile(r"(/api/v1/camera/vendor/[a-z]+/)[A-Za-z0-9_\-]+")
# ключи/секреты: длинные буквенно-цифровые строки без дефисов (UUID и hex-хеши с дефисами не задевает)
_LONG_KEY = re.compile(r"\b(?=[A-Za-z0-9_]{40,}\b)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_]{40,}\b")


def mask_camera_key(value: str) -> str:
    return CAMERA_KEY_PATH.sub(r"\1***", value)


def redact_text(value: str) -> str:
    value = mask_camera_key(value)
    value = _JWT.sub(REDACTED, value)
    value = _BEARER.sub("Bearer " + REDACTED, value)
    value = _PLATE.sub(REDACTED, value)
    return _LONG_KEY.sub(REDACTED, value)


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEYS.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact(v) for v in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def redact_value(value: Any, key: str | None = None) -> Any:
    """Публичный редактор для Sentry и любых внешних отправок: те же правила, что и для логов."""
    return _redact(value, key)


def redact_processor(_logger: object, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        event_dict[key] = _redact(event_dict[key], key)
    return event_dict


def trace_processor(_logger: object, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """trace_id/span_id текущего спана (§1) и `message` как каноническое имя поля события для Loki."""
    trace_id, span_id = current_trace_ids()
    if trace_id is not None:
        event_dict.setdefault("trace_id", trace_id)
        event_dict.setdefault("span_id", span_id)
    if "message" not in event_dict and "event" in event_dict:
        event_dict["message"] = event_dict["event"]
    return event_dict


def _orjson_dumps(value: Any, **_: Any) -> str:
    return orjson.dumps(value, default=str).decode()


def configure_logging(
    *, service: str, environment: str, version: str, level: str = "INFO", json_output: bool = True
) -> None:
    """Настраивает structlog + stdlib. JSON по умолчанию; в dev можно отключить json_output для читаемости."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _static_fields({"service": service, "environment": environment, "version": version}),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        trace_processor,
        redact_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer(serializer=_orjson_dumps) if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")


def _static_fields(fields: Mapping[str, str]) -> Any:
    def processor(_logger: object, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        for k, v in fields.items():
            event_dict.setdefault(k, v)
        return event_dict

    return processor


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
