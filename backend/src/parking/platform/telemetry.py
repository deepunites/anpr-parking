"""Единая инициализация наблюдаемости процесса: логи (structlog), трассировка (OTel), Sentry, метрики."""

from __future__ import annotations

import re
from typing import Any

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace.export import SpanExporter

from parking.platform.config import Settings
from parking.platform.db.engine import Database
from parking.platform.logging import configure_logging, get_logger
from parking.platform.sentry import init_sentry
from parking.platform.tracing import configure_tracing, instrument_libraries

log = get_logger("telemetry")
_instrumented = False


def _exporter(settings: Settings) -> SpanExporter | None:
    if not settings.otel_exporter_endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415

    return OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint.rstrip("/") + "/v1/traces")


def bootstrap_telemetry(settings: Settings, *, service: str, exporter: SpanExporter | None = None) -> None:
    """Логи + трассировка + Sentry для API или воркера. Идемпотентно для инструментации библиотек."""
    global _instrumented  # noqa: PLW0603 — инструментация библиотек — один раз на процесс
    configure_logging(
        service=service, environment=settings.env.value, version=settings.version, level=settings.log_level
    )
    configure_tracing(
        service=service,
        environment=settings.env.value,
        version=settings.version,
        exporter=exporter or _exporter(settings),
        entry_ratio=settings.otel_entry_sample_ratio,
        api_ratio=settings.otel_api_sample_ratio,
    )
    if not _instrumented:
        instrument_libraries()
        _instrumented = True
    if init_sentry(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.env.value,
        release=f"parking@{settings.version}",
        traces_sample_rate=settings.sentry_traces_sample_rate,
    ):
        log.info("sentry_enabled", release=settings.version)


def instrument_app(app: Any) -> None:
    """Серверный спан на HTTP-запрос. Внутренние спаны `http send`/`http receive` на каждое ASGI-сообщение выключены:
    под нагрузкой они давали ≈7 % CPU процесса API (особенно на WebSocket-потоках), а в трассах не нужны —
    длительность ответа видна по серверному спану. Служебные и WS-маршруты не трассируются."""
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="health,metrics,ws/",
        exclude_spans=["receive", "send"],
        server_request_hook=redact_camera_key,
    )


_CAMERA_KEY_PATH = re.compile(r"(/api/v1/camera/vendor/[a-z]+/)[^/?#]+")


def redact_camera_key(span: Any, scope: dict[str, Any]) -> None:
    """Камеры Hikvision/Dahua не умеют свой заголовок — ключ приходит последним сегментом пути. В трассы он не
    попадает: атрибуты пути/URL перезаписываются маской (журнал запросов и так пишет шаблон роута)."""
    path = str(scope.get("path", ""))
    if span is None or not _CAMERA_KEY_PATH.search(path):
        return
    masked = _CAMERA_KEY_PATH.sub(r"\1***", path)
    for attribute in ("http.target", "url.path"):
        span.set_attribute(attribute, masked)
    for attribute in ("http.url", "url.full"):
        span.set_attribute(attribute, masked)


def instrument_database(database: Database) -> None:
    for pool, engine in database.engines.items():
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, enable_commenter=False)
        log.debug("sqlalchemy_instrumented", pool=pool.value)
