"""Перехват логов для тестов наблюдаемости: та же цепочка процессоров, что в проде (contextvars → trace → redact),
но вместо рендерера — накопитель словарей. structlog.testing.capture_logs не годится: он выбрасывает merge_contextvars."""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from typing import Any

import structlog

from parking.platform.logging import redact_processor, trace_processor


@contextlib.contextmanager
def capture_logs() -> Iterator[list[dict[str, Any]]]:
    previous = structlog.get_config()
    capture = structlog.testing.LogCapture()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            trace_processor,
            redact_processor,
            capture,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    try:
        yield capture.entries
    finally:
        structlog.configure(**previous)
