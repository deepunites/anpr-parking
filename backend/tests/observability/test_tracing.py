"""Фаза 7 §3: трасса EXIT-саги непрерывна сквозь Redis Stream (traceparent в конверте), EXIT экспортируется
целиком независимо от head-based сэмплинга; ENTRY при ratio=0 не экспортируется, но trace_id в логах есть."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision
from opentelemetry.trace import SpanKind

from parking.platform.tracing import (
    FLOW_ATTR,
    FORCE_EXPORT_ATTR,
    ParkingSampler,
    SelectiveExportProcessor,
    configure_tracing,
    context_from_traceparent,
    current_traceparent,
    span,
)
from parking.runtime.container import Container
from parking.workers.outbox_publisher import OutboxPublisher
from tests.observability.logcapture import capture_logs
from tests.observability.test_correlation_end_to_end import _consumer, publisher  # noqa: F401 — фикстура
from tests.support.camera_fixtures import event_body, send_event, unique_plate

TRACE_DUMP = Path(__file__).resolve().parents[3] / "docs" / "backend" / "trace-exit-saga.json"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = configure_tracing(service="test", environment="test", version="0", exporter=exporter)
    assert isinstance(provider, TracerProvider)
    return exporter


def _dump(spans: list[ReadableSpan]) -> list[dict[str, Any]]:
    out = []
    for s in sorted(spans, key=lambda s: s.start_time or 0):
        out.append(
            {
                "name": s.name,
                "trace_id": f"{s.context.trace_id:032x}",
                "span_id": f"{s.context.span_id:016x}",
                "parent_span_id": f"{s.parent.span_id:016x}" if s.parent else None,
                "kind": s.kind.name,
                "status": s.status.status_code.name,
                "duration_ms": round(((s.end_time or 0) - (s.start_time or 0)) / 1e6, 3),
                "attributes": {k: v for k, v in (s.attributes or {}).items() if not k.startswith("net.")},
            }
        )
    return out


async def test_exit_saga_trace_is_continuous_across_stream(
    client: httpx.AsyncClient,
    container: Container,
    publisher: OutboxPublisher,  # noqa: F811
    exporter: InMemorySpanExporter,
    entry_key: str,
    exit_key: str,
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    await owner.execute("UPDATE outbox SET status = 'PUBLISHED', published_at = now() WHERE status = 'PENDING'")
    consumer = _consumer(container)
    await consumer.ensure_groups()

    async def drain() -> None:
        for _ in range(6):
            await publisher.publish_batch()
            await consumer.poll_once(block_ms=1)

    plate = unique_plate()
    entry = await send_event(client, entry_key, event_body(plate))
    assert entry.status_code == 200, entry.text
    await drain()
    exporter.clear()
    exit_ = await send_event(
        client, exit_key, event_body(plate, event_type="EXIT", camera_code="EXIT_01", event_time=datetime.now(tz=UTC))
    )
    assert exit_.status_code == 200, exit_.text
    await drain()
    saga = await owner.fetchrow(
        "SELECT state::text AS state FROM exit_saga WHERE session_id = $1", exit_.json()["session_id"]
    )
    assert saga is not None and saga["state"] == "COMPLETED"

    spans = list(exporter.get_finished_spans())
    root = next(s for s in spans if s.name == "exit.start")
    same_trace = [s for s in spans if s.context.trace_id == root.context.trace_id]
    names = {s.name for s in same_trace}
    expected = {"exit.start", "exit.saga.step", "barrier.open", "consume devices.barrier.command_requested.v1"}
    assert expected <= names, f"в трассе EXIT нет спанов {expected - names}; есть: {sorted(names)}"
    server = [s for s in same_trace if s.kind is SpanKind.SERVER]
    assert server and server[0].attributes and server[0].attributes.get(FORCE_EXPORT_ATTR) is True, (
        "серверный спан POST /camera/events для EXIT экспортируется целиком (mark_exit_flow)"
    )
    consumer_spans = [s for s in same_trace if s.name.startswith("consume ")]
    ids = {s.context.span_id for s in same_trace}
    assert all(s.parent is not None and s.parent.span_id in ids for s in consumer_spans), (
        "родитель спана потребителя — спан API-запроса той же трассы (traceparent из конверта)"
    )
    saga_steps = [s for s in same_trace if s.name == "exit.saga.step"]
    assert {str(s.attributes.get("saga.state")) for s in saga_steps if s.attributes} >= {"COMPLETED"}
    assert not any(k in (s.attributes or {}) for s in spans for k in ("plate", "plate_number")), "I-10: без номеров"
    TRACE_DUMP.parent.mkdir(parents=True, exist_ok=True)
    TRACE_DUMP.write_text(json.dumps(_dump(same_trace), indent=2, ensure_ascii=False, default=str) + "\n")
    print(f"EXIT trace {root.context.trace_id:032x}: {len(same_trace)} spans → {TRACE_DUMP}")


async def test_entry_trace_not_exported_but_has_trace_id(
    client: httpx.AsyncClient, exporter: InMemorySpanExporter, entry_key: str, clean_lot: None
) -> None:
    """ENTRY при entry_ratio=0 (тестовые настройки) → RECORD_ONLY: ни один спан запроса не экспортируется,
    но trace_id в логах есть (спан пишется), а ошибки экспортировались бы (SelectiveExportProcessor)."""
    exporter.clear()
    with capture_logs() as entries:
        for _ in range(5):
            resp = await send_event(client, entry_key, event_body(unique_plate()))
            assert resp.status_code == 200, resp.text
    assert [s.name for s in exporter.get_finished_spans()] == []
    accepted = [e for e in entries if e["event"] == "anpr_event_accepted"]
    assert len(accepted) == 5 and all(len(e["trace_id"]) == 32 and len(e["span_id"]) == 16 for e in accepted)
    assert len({e["trace_id"] for e in accepted}) == 5, "каждый запрос — своя трасса"


def test_sampler_decisions() -> None:
    sampler = ParkingSampler(entry_ratio=0.0, api_ratio=0.0)
    assert sampler.should_sample(None, 1, "x", attributes={FLOW_ATTR: "exit"}).decision is Decision.RECORD_AND_SAMPLE
    assert sampler.should_sample(None, 1, "GET", attributes={"http.target": "/health"}).decision is Decision.DROP
    assert sampler.should_sample(None, 1, "GET", attributes={"http.target": "/metrics"}).decision is Decision.DROP
    assert (
        sampler.should_sample(None, 1, "POST", attributes={"http.target": "/api/v1/camera/events"}).decision
        is Decision.RECORD_ONLY
    )
    assert (
        sampler.should_sample(None, 1, "GET", attributes={"http.target": "/api/v1/lots"}).decision
        is Decision.RECORD_ONLY
    )
    everything = ParkingSampler(entry_ratio=1.0, api_ratio=1.0)
    assert (
        everything.should_sample(None, 1, "GET", attributes={"http.target": "/api/v1/lots"}).decision
        is Decision.RECORD_AND_SAMPLE
    )
    # решение родителя из traceparent наследуется
    sampled_parent = context_from_traceparent("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
    unsampled_parent = context_from_traceparent("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00")
    assert sampler.should_sample(sampled_parent, 1, "child").decision is Decision.RECORD_AND_SAMPLE
    assert sampler.should_sample(unsampled_parent, 1, "child").decision is Decision.RECORD_ONLY


def test_error_spans_are_exported_even_when_unsampled(exporter: InMemorySpanExporter) -> None:
    exporter.clear()
    processor = SelectiveExportProcessor(exporter)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(processor)
    with span("unsampled.ok", tenant_id="t"):
        pass
    with pytest.raises(RuntimeError), span("unsampled.error", tenant_id="t"):
        raise RuntimeError("boom")
    names = [s.name for s in exporter.get_finished_spans()]
    assert "unsampled.error" in names and "unsampled.ok" not in names


def test_span_forbids_sensitive_attributes() -> None:
    with pytest.raises(ValueError, match="plate"), span("x", plate="01A123BC"):
        pass
    with pytest.raises(ValueError, match="token"), span("x", access_token="abc"):
        pass


def test_traceparent_roundtrip(exporter: InMemorySpanExporter) -> None:
    assert current_traceparent() is None or isinstance(current_traceparent(), str)
    with span("root", **{FLOW_ATTR: "exit"}):
        tp = current_traceparent()
        assert tp is not None and tp.startswith("00-") and int(tp[-2:], 16) & 0x01, "флаг sampled (W3C)"
        ctx = context_from_traceparent(tp)
        assert ctx is not None and trace.get_current_span(ctx).get_span_context().is_valid
    assert context_from_traceparent(None) is None and context_from_traceparent("garbage") is None
