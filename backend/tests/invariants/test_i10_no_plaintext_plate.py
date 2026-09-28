"""I-10. Номер в открытом виде не хранится ни в одной колонке, не пишется в лог, метрику, трейс и Sentry.

Механизм: в БД только plate_hash (HMAC) / *_encrypted (AES-GCM, bytea); PlateNumber.__str__/__repr__ маскирует;
structlog redact_processor (по ключу и по значению) перед рендерером; лейблы метрик проверяются тестом
кардинальности (запрещённый набор); span() отвергает атрибуты plate*; Sentry before_send редактирует событие.
При нарушении: утечка ПДн владельцев через любой из каналов, нарушающая закон о персональных данных.
"""

from __future__ import annotations

import io
import json

import asyncpg
import pytest
import structlog
from prometheus_client import generate_latest

from parking.platform import metrics
from parking.platform.logging import configure_logging, get_logger
from parking.platform.sentry import scrub_event
from parking.platform.tracing import span
from parking.shared.plate import PlateNumber
from tests.db.test_no_plaintext_plate import ALLOWED, MASKED

PLATE = "01A123BC"


async def test_database_has_no_plaintext_plate_column(owner: asyncpg.Connection) -> None:
    rows = await owner.fetch(
        """
        SELECT c.relname AS t, a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND a.attnum > 0
          AND NOT a.attisdropped AND (a.attname ILIKE '%plate%' OR a.attname ILIKE '%number%')
        """
    )
    for r in rows:
        assert (r["t"], r["col"]) in ALLOWED, f"неизвестная колонка с номером: {r['t']}.{r['col']}"
        if r["col"] != "plate_format" and (r["t"], r["col"]) not in MASKED:
            assert r["typ"] == "bytea" and r["col"].endswith(("_hash", "_encrypted")), dict(r)


def test_plate_value_object_masks_itself() -> None:
    plate = PlateNumber.parse(PLATE)
    assert PLATE not in str(plate) and PLATE not in repr(plate) and PLATE not in f"{plate}"
    assert plate.value == PLATE, "открытое значение доступно только явно через .value"


def test_logger_output_never_contains_plate() -> None:
    buffer = io.StringIO()
    previous = structlog.get_config()
    try:
        configure_logging(service="t", environment="test", version="0", level="DEBUG")
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buffer))
        log = get_logger("i10")
        log.info("event", plate=PLATE, note=f"seen {PLATE}", nested={"plate_number": PLATE}, items=[PLATE])
        log.error("boom", exc_info=ValueError(f"bad plate {PLATE}"))
    finally:
        structlog.configure(**previous)
    output = buffer.getvalue()
    assert PLATE not in output, output
    assert json.loads(output.splitlines()[0])["plate"] == "[REDACTED]"


def test_metrics_exposition_has_no_plate_label() -> None:
    """Имена метрик и лейблов без «plate»; значения лейблов вроде reason="INVALID_PLATE" — не ПДн; ни одно
    значение лейбла не похоже на номер."""
    from prometheus_client.parser import text_string_to_metric_families

    from parking.platform.logging import redact_text

    for family in text_string_to_metric_families(generate_latest(metrics.registry).decode()):
        assert "plate" not in family.name
        for sample in family.samples:
            assert not any("plate" in label for label in sample.labels)
            for value in sample.labels.values():
                assert redact_text(value) == value, f"значение лейбла похоже на номер: {sample}"


def test_span_rejects_plate_attributes() -> None:
    for key in ("plate", "plate_number", "raw_plate", "PLATE"):
        with pytest.raises(ValueError, match="forbidden"), span("x", **{key: PLATE}):
            pass


def test_sentry_event_is_scrubbed() -> None:
    event = scrub_event({"message": f"plate {PLATE}", "extra": {"plate": PLATE, "text": f"{PLATE} arrived"}})
    assert event is not None and PLATE not in json.dumps(event)
