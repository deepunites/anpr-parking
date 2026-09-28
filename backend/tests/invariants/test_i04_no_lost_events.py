"""I-4. Событие, на которое API вернул 2xx, доходит до PostgreSQL или до DLQ/оффлайн-буфера — но не исчезает.

Механизм: запись anpr_event в той же транзакции, что и ответ (200), либо XADD в Redis Stream anpr:offline до ответа
(202) при открытом DB-breaker; если и буфер недоступен/переполнен — 503, а не 2xx. Replay буфера идемпотентен по event_id.
При нарушении: камера получает 2xx, а события нет ни в БД, ни в стриме — потерянный въезд/выезд без следа.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import httpx
import pytest

from parking.runtime.container import Container
from tests.support.camera_fixtures import clean_streams, event_body, send_event, unique_plate  # noqa: F401 — фикстура


async def test_200_means_row_in_postgres(client: httpx.AsyncClient, entry_key: str, owner: asyncpg.Connection) -> None:
    body = event_body(unique_plate())
    resp = await send_event(client, entry_key, body)
    assert resp.status_code == 200, resp.text
    event_id = UUID(resp.json()["event_id"])
    assert await owner.fetchval("SELECT count(*) FROM anpr_event WHERE id = $1", event_id) == 1


async def test_202_means_entry_in_offline_stream(
    client: httpx.AsyncClient,
    container: Container,
    entry_key: str,
    clean_streams: None,  # noqa: F811
) -> None:
    breaker = container.db_breaker
    for _ in range(breaker.threshold):
        breaker.record_failure()  # БД «недоступна»: breaker OPEN без реальной остановки PostgreSQL
    try:
        resp = await send_event(client, entry_key, event_body(unique_plate()))
        assert resp.status_code == 202, resp.text
        event_id = resp.json()["event_id"]
        entries = await container.redis.xrange("anpr:offline")
        assert any(f[b"event_id"].decode() == event_id for _, f in entries), "202 без XADD — событие потеряно"
    finally:
        breaker.record_success()


async def test_buffer_unavailable_is_503_not_2xx(
    client: httpx.AsyncClient, container: Container, entry_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = container.db_breaker
    for _ in range(breaker.threshold):
        breaker.record_failure()

    async def broken(*_: object, **__: object) -> int:
        raise ConnectionError("redis down")

    monkeypatch.setattr(container.offline_buffer, "enqueue", broken)
    try:
        resp = await send_event(client, entry_key, event_body(unique_plate()))
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] in {"SERVICE_UNAVAILABLE", "UNAVAILABLE"}, resp.text
    finally:
        breaker.record_success()
