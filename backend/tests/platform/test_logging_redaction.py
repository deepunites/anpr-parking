"""I-10: номер и JWT не попадают в вывод логгера ни по ключу, ни по значению, ни во вложенных структурах."""

from __future__ import annotations

import io
import json

import structlog

from parking.platform.logging import REDACTED, configure_logging, get_logger, redact_processor, redact_text

JWT = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwidGVuYW50X2lkIjoiYWJjIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
PLATES = ["01A123AA", "01 A 123 AA", "01А123АА", "01123AAA"]
CAMERA_KEY = "devkey8f3a0c1e5b7d9a2c4e6f8a0b1c3d5e7f9a1b3c5d7e9f1a2b"


def test_processor_redacts_by_key_and_by_value() -> None:
    event = {
        "event": f"login for 01A123AA with {JWT}",
        "password": "hunter2",
        "plate": "01A123AA",
        "x_camera_key": CAMERA_KEY,
        "authorization": "Bearer abc.def.ghi",
        "nested": {"token": "t", "list": [{"plate_number": "01A123AA"}, "01 A 123 AA", f"Bearer {JWT}"]},
        "plate_hash": "ab" * 32,
        "session_id": "0f2a5b1c-1111-4111-8111-111111111111",
        "amount_minor": 1500000,
    }
    out = json.dumps(redact_processor(None, "info", event), ensure_ascii=False)
    for plate in PLATES:
        assert plate not in out
    assert JWT not in out and "hunter2" not in out and CAMERA_KEY not in out and "abc.def.ghi" not in out
    assert event["plate_hash"] == "ab" * 32, "хеш номера — не ПДн, остаётся"
    assert event["session_id"].startswith("0f2a5b1c") and event["amount_minor"] == 1500000
    assert event["password"] == REDACTED and event["plate"] == REDACTED


def test_end_to_end_logger_output(monkeypatch: object) -> None:
    stream = io.StringIO()
    configure_logging(service="test", environment="dev", version="0", level="DEBUG")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=stream))
    log = get_logger("test")
    log.info("camera event", plate="01A123AA", jwt=JWT, note=f"driver 01 A 123 AA presented {JWT}", api_key=CAMERA_KEY)
    output = stream.getvalue()
    assert "camera event" in output
    for secret in [*PLATES, JWT, CAMERA_KEY]:
        assert secret not in output
    record = json.loads(output)
    assert record["service"] == "test" and record["level"] == "info" and "timestamp" in record


def test_redact_text_keeps_uuids_and_hex_hashes() -> None:
    text = "session 0f2a5b1c-1111-4111-8111-111111111111 hash " + "ab" * 32
    assert redact_text(text) == text
