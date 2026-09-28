"""OpenTelemetry (фаза 7 §3): автоинструментация FastAPI/SQLAlchemy/httpx/Redis/Celery, ручные спаны на границах
доменных операций, W3C traceparent через outbox → Redis Streams → консьюмер и через заголовки Celery.

Сэмплирование (`ParkingSampler` + `SelectiveExportProcessor`):
- EXIT-саги — 100 %: спаны с атрибутом `parking.flow=exit` (ingest помечает EXIT, консьюмер — события саги/платежей/
  шлагбаума) всегда RECORD_AND_SAMPLE, даже если корневой HTTP-спан не выбран;
- ошибки — 100 %: спаны в статусе ERROR экспортируются процессором независимо от решения сэмплера (спан пишется
  как RECORD_ONLY и «поднимается» при ошибке — head-based сэмплер не знает исход заранее);
- успешные ENTRY (`POST /api/v1/camera/events` без пометки exit) — `otel_entry_sample_ratio` (по умолчанию 2 %):
  при 3 млн событий/сутки (§3 CORE) и ~6 спанов на трассу это ≈ 360 тыс. спанов/сутки ≈ 150 МБ/сутки в Tempo —
  дешёво, а 2 % достаточно для p95 по площадкам; 100 % дало бы 18 млн спанов/сутки — бессмысленно, EXIT и ошибки
  и так полные;
- health/metrics/websocket — не трассируются (DROP).
Атрибуты: tenant_id, lot_id, session_id, camera_id, event_type. Номер — никогда (`span()` отвергает ключ plate).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult, TraceIdRatioBased
from opentelemetry.trace import Link, SpanKind, StatusCode, get_current_span

FLOW_ATTR = "parking.flow"
FORCE_EXPORT_ATTR = "parking.force_export"
_DROP_PREFIXES = ("/health", "/metrics", "/ws/")
_ENTRY_PATH = "/api/v1/camera/events"
_FORBIDDEN_ATTRS = re.compile(
    r"plate(?!_hash|_format)|password|secret|token|api[_-]?key|authorization|cookie|pepper", re.IGNORECASE
)


class ParkingSampler(Sampler):
    def __init__(self, *, entry_ratio: float = 0.02, api_ratio: float = 0.1) -> None:
        self._entry = TraceIdRatioBased(entry_ratio)
        self._api = TraceIdRatioBased(api_ratio)

    def should_sample(
        self,
        parent_context: otel_context.Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Mapping[str, Any] | None = None,
        links: Sequence[Link] | None = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        attrs = dict(attributes or {})
        if attrs.get(FLOW_ATTR) == "exit":
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attrs)
        parent = get_current_span(parent_context).get_span_context()
        if parent.is_valid:  # решение родителя (в т. ч. восстановленного из traceparent) наследуется
            return SamplingResult(self._child_decision(parent.trace_flags.sampled, kind, attrs), attrs)
        target = str(attrs.get("http.target") or attrs.get("url.path") or attrs.get("http.route") or "")
        if any(target.startswith(p) for p in _DROP_PREFIXES):
            return SamplingResult(Decision.DROP, attrs)
        ratio = self._entry if target.startswith(_ENTRY_PATH) else self._api
        sampled = ratio.should_sample(parent_context, trace_id, name, kind, attributes, links).decision
        if sampled is Decision.RECORD_AND_SAMPLE:
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attrs)
        return SamplingResult(Decision.RECORD_ONLY, attrs)  # пишется, экспортируется только при ошибке/force

    @staticmethod
    def _child_decision(parent_sampled: bool, kind: SpanKind | None, attrs: Mapping[str, Any]) -> Decision:
        if parent_sampled:
            return Decision.RECORD_AND_SAMPLE
        # несэмплированная трасса: клиентские спаны автоинструментации (каждый SQL/Redis-вызов — 10+ на запрос)
        # не пишутся вовсе — экономит CPU на приёме событий; свои спаны (parking.*) остаются RECORD_ONLY,
        # чтобы ошибка внутри них всё равно экспортировалась (SelectiveExportProcessor)
        if "db.system" in attrs or kind is SpanKind.CLIENT:
            return Decision.DROP
        return Decision.RECORD_ONLY

    def get_description(self) -> str:
        return "ParkingSampler(exit=100%, errors=100%, entry=ratio, api=ratio)"


class SelectiveExportProcessor(SpanProcessor):
    """Экспортирует спаны, выбранные сэмплером, а также любые спаны в статусе ERROR или с parking.force_export."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter
        self.exported = 0

    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        forced = bool((span.attributes or {}).get(FORCE_EXPORT_ATTR)) or span.status.status_code is StatusCode.ERROR
        if (ctx is not None and ctx.trace_flags.sampled) or forced:
            self._exporter.export([span])
            self.exported += 1

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def configure_tracing(
    *,
    service: str,
    environment: str,
    version: str,
    exporter: SpanExporter | None,
    entry_ratio: float = 0.02,
    api_ratio: float = 0.1,
) -> TracerProvider:
    """Провайдер трассировки процесса; exporter=None — спаны не экспортируются (dev без коллектора),
    контекст и trace_id в логах всё равно работают."""
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # глобальный провайдер задаётся один раз на процесс (ограничение OTel SDK): повторный вызов
        # (тесты, несколько create_app) лишь добавляет экспортёр к существующему провайдеру
        if exporter is not None:
            current.add_span_processor(SelectiveExportProcessor(exporter))
        return current
    resource = Resource.create(
        {"service.name": service, "deployment.environment": environment, "service.version": version}
    )
    provider = TracerProvider(resource=resource, sampler=ParkingSampler(entry_ratio=entry_ratio, api_ratio=api_ratio))
    if exporter is not None:
        provider.add_span_processor(SelectiveExportProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def instrument_libraries() -> None:
    """Автоинструментация клиентов; FastAPI/SQLAlchemy инструментируются по месту (app, engine)."""
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Ручной спан доменной границы. Атрибуты с номером/секретами запрещены на уровне API."""
    for key in attributes:
        if _FORBIDDEN_ATTRS.search(key):
            raise ValueError(f"span attribute {key} is forbidden (PII/secret)")
    clean: dict[str, str | int | float | bool] = {
        k: (v if isinstance(v, str | int | float | bool) else str(v)) for k, v in attributes.items() if v is not None
    }
    with tracer("parking").start_as_current_span(name, attributes=clean) as current:
        try:
            yield current
        except Exception as exc:
            current.set_status(StatusCode.ERROR, type(exc).__name__)
            current.record_exception(exc)
            raise


def mark_exit_flow() -> None:
    """Ingest узнал, что событие — EXIT: корневой спан экспортируется целиком (100 % EXIT-саг)."""
    current = get_current_span()
    if current.is_recording():
        current.set_attribute(FLOW_ATTR, "exit")
        current.set_attribute(FORCE_EXPORT_ATTR, True)


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> otel_context.Context | None:
    if not traceparent:
        return None
    ctx = extract({"traceparent": traceparent})
    return ctx if get_current_span(ctx).get_span_context().is_valid else None  # мусор в конверте → новая трасса


def current_trace_ids() -> tuple[str | None, str | None]:
    ctx = get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
