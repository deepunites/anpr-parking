"""v2 этап 8: пропускная способность журнала событий камер (буфер процесса → пакетная запись пулом bulk).

Приём события добавляет строку в память (микросекунды, SQL в горячем пути нет — tests/anpr/test_sql_budget.py);
здесь меряется, успевает ли запись: 10 000 строк двух клиентов пакетами по 200 и что ни одна не потеряна."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg

from parking.modules.integrations.application.contracts import JournalEntry, Outcome
from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN, LOT_A1, TENANT_A, TENANT_B

ROWS = 10_000


async def test_journal_writes_10k_rows_without_loss(container: Container, owner: asyncpg.Connection) -> None:
    marker = uuid4()
    now = datetime.now(tz=UTC)
    t0 = time.perf_counter()
    for i in range(ROWS):
        container.journal.enqueue(
            JournalEntry(
                tenant_id=TENANT_A if i % 5 else TENANT_B,
                lot_id=LOT_A1 if i % 5 else None,
                camera_id=CAM_A1_IN if i % 5 else None,
                source="load",
                outcome=Outcome.REJECTED if i % 3 == 0 else Outcome.ACCEPTED,
                received_at=now,
                status="LOW_CONFIDENCE" if i % 3 == 0 else "ENTRY_CONFIRMED",
                event_type="ENTRY",
                plate_masked="01A***AA",
                correlation_id=marker,
                http_status=422 if i % 3 == 0 else 200,
            )
        )
    enqueue_us = (time.perf_counter() - t0) / ROWS * 1e6
    t1 = time.perf_counter()
    while container.journal._buffer:
        await container.journal.flush()
    flush_s = time.perf_counter() - t1
    written = await owner.fetchval("SELECT count(*) FROM event_journal WHERE correlation_id = $1", marker)
    print(
        f"\nGATE journal: enqueue {enqueue_us:.1f} µs/row, flush {ROWS} rows {flush_s:.2f} s ({ROWS / flush_s:.0f} rows/s), written={written}, dropped={container.journal.dropped}"
    )
    assert written == ROWS and container.journal.dropped == 0
    assert enqueue_us < 200  # приём события не ждёт журнал
    assert ROWS / flush_s > 500  # запас над пиком стенда-эталона (§6: сотни событий/с на весь кластер)
