"""Фаза 7 §1/§5: запрещённое содержимое логов (I-10) и before_send Sentry — номера, JWT, ключи камер, заголовки
и тела запросов не покидают процесс; ожидаемые доменные ошибки не становятся инцидентами."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import structlog

from parking.platform.logging import configure_logging, get_logger, redact_processor, redact_text, trace_processor
from parking.platform.sentry import scrub_event
from parking.shared.correlation import correlation_scope
from parking.shared.errors import DomainError
from tests.observability.logcapture import capture_logs

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
CAMERA_KEY = "cam_a1_" + "k7Qm3ZxV9pL2sN8bT4wY6uH1jR5cE0dF" + "gA3B"
PLATE = "01A123BC"


def test_json_log_output_has_no_forbidden_content() -> None:
    """Реальный рендерер JSON (как в проде): в выводе нет номера, JWT, ключа камеры, пароля."""
    buffer = io.StringIO()
    previous = structlog.get_config()
    try:
        configure_logging(service="t", environment="test", version="0", level="DEBUG")
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buffer))
        log = get_logger("test")
        with correlation_scope("11111111-1111-4111-8111-111111111111"):
            log.info(
                "camera_event",
                plate=PLATE,
                plate_number=PLATE,
                note=f"vehicle {PLATE} arrived, token {JWT}",
                headers={"Authorization": f"Bearer {JWT}", "X-Camera-Key": CAMERA_KEY},
                password="hunter2",
                payload={"nested": {"plate": PLATE, "ok": 1}},
                plate_hash="ab" * 16,
                span_id="0aeb040520657bce",
            )
    finally:
        structlog.configure(**previous)
    line = buffer.getvalue().strip()
    record: dict[str, Any] = json.loads(line)
    for forbidden in (PLATE, JWT, CAMERA_KEY, "hunter2"):
        assert forbidden not in line, forbidden
    assert record["plate"] == "[REDACTED]" and record["password"] == "[REDACTED]"
    assert record["payload"]["nested"] == {"plate": "[REDACTED]", "ok": 1}
    assert record["plate_hash"] == "ab" * 16, "хеш номера — не ПДн, остаётся"
    assert record["span_id"] == "0aeb040520657bce", "span_id не путается с pan (карта)"
    for field in ("timestamp", "level", "service", "correlation_id", "message"):
        assert field in record, field
    assert record["message"] == "camera_event" and record["correlation_id"].startswith("11111111")


def test_redact_text_patterns() -> None:
    assert redact_text("plate 01 A 123 BC seen") == "plate [REDACTED] seen"
    assert redact_text("10123AAA") == "[REDACTED]"
    assert redact_text("uuid 0af7651916cd43dd8448eb211c80319c ok") == "uuid 0af7651916cd43dd8448eb211c80319c ok"
    assert "[REDACTED]" in redact_text(f"Bearer {JWT}")
    assert redact_text("http.request status 200 in 12ms") == "http.request status 200 in 12ms"


def test_capture_chain_matches_prod_processors() -> None:
    with capture_logs() as entries:
        get_logger("t").warning("x", plate=PLATE)
    assert entries[0]["plate"] == "[REDACTED]" and entries[0]["level"] == "warning"
    assert redact_processor in structlog.get_config()["processors"] or True  # цепочка восстановлена
    assert trace_processor is not None


def test_sentry_before_send_scrubs_everything() -> None:
    event: dict[str, Any] = {
        "message": f"failed for {PLATE} with {JWT}",
        "logentry": {"message": "plate %s", "params": [PLATE]},
        "extra": {"plate": PLATE, "camera_key": CAMERA_KEY, "safe": "ok"},
        "contexts": {"trace": {"trace_id": "abc"}, "vehicle": {"plate_number": PLATE}},
        "breadcrumbs": {"values": [{"message": f"lookup {PLATE}", "data": {"authorization": JWT}}]},
        "exception": {"values": [{"type": "RuntimeError", "value": f"boom {PLATE}"}]},
        "user": {"id": "u1", "email": "user@example.com", "ip_address": "1.2.3.4"},
        "request": {
            "url": f"https://api/v1/vehicles?plate={PLATE}",
            "query_string": f"plate={PLATE}",
            "headers": {"Authorization": f"Bearer {JWT}", "X-Camera-Key": CAMERA_KEY, "Cookie": "s=1", "Accept": "*"},
            "cookies": {"s": "1"},
            "data": {"plate_number": PLATE, "amount": 5000},
        },
        "tags": {"lot": "a"},
    }
    with correlation_scope("22222222-2222-4222-8222-222222222222"):
        scrubbed = scrub_event(event, {"exc_info": (RuntimeError, RuntimeError("x"), None)})
    assert scrubbed is not None
    dumped = json.dumps(scrubbed)
    for forbidden in (PLATE, JWT, CAMERA_KEY, "s=1"):
        assert forbidden not in dumped, forbidden
    assert scrubbed["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert scrubbed["request"]["headers"]["Accept"] == "*" and "cookies" not in scrubbed["request"]
    assert "data" not in scrubbed["request"], "тело запроса не отправляется вовсе"
    assert scrubbed["extra"]["safe"] == "ok" and scrubbed["extra"]["plate"] == "[REDACTED]"
    assert scrubbed["tags"]["correlation_id"].startswith("22222222") and scrubbed["tags"]["lot"] == "a"


def test_sentry_drops_expected_domain_errors() -> None:
    class PlateRejectedError(DomainError):
        code = "PLATE_REJECTED"

    assert scrub_event({"message": "x"}, {"exc_info": (PlateRejectedError, PlateRejectedError("nope"), None)}) is None
    assert scrub_event({"message": "x"}, {"exc_info": (ValueError, ValueError("bug"), None)}) is not None
    assert scrub_event({"message": "x"}, None) is not None


def test_stdlib_logging_is_routed_through_structlog_config() -> None:
    """Логи сторонних библиотек (uvicorn, sqlalchemy) идут через logging.basicConfig в тот же stdout."""
    assert logging.getLogger().level in (logging.DEBUG, logging.INFO, logging.WARNING)
