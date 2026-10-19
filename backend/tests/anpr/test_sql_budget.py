"""Бюджет SQL на camera-событие (долг §4 п.1 PRODUCTION_READINESS).

До оптимизации ENTRY стоил 19 операторов, EXIT — 16 (профиль фазы 8). Тест фиксирует установившийся режим (ключи камер
и справочники прогреты) и падает, если новая правка снова добавляет запросов на горячий путь.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import event

from parking.modules.tenancy.application.lot_service import UpdateLotCommand
from parking.modules.tenancy.infrastructure.repositories import SqlLotDirectory
from parking.platform.db.engine import Pool
from parking.runtime.container import Container
from tests.conftest import LOT_A1, TENANT_A, TENANT_B
from tests.support.camera_fixtures import ADMIN_A, event_body, send_event, unique_plate

pytestmark = pytest.mark.usefixtures("clean_streams")

ENTRY_BUDGET = 11  # set_config, lock, реестр, vehicle, эхо выезда, session, barrier_command, anpr_event, реестр, outbox (+ правила)
EXIT_BUDGET = 11  # set_config, lock, реестр, сессия, сага, предоплата, визиты за 24 ч, сага, anpr_event, реестр, outbox


@contextmanager
def sql_log(container: Container) -> Iterator[list[str]]:
    statements: list[str] = []

    def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        statements.append(re.sub(r"\s+", " ", statement).strip())

    # журнал событий (v2 этап 6) пишется пачкой раз в секунду через пул bulk, вне транзакции события — в бюджет
    # горячего пути не входит; иначе его периодический сброс случайно попадал бы в окно замера
    engines = [e.sync_engine for pool, e in container.database.engines.items() if pool is not Pool.BULK]
    for e in engines:
        event.listen(e, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        for e in engines:
            event.remove(e, "before_cursor_execute", _before)


async def test_entry_and_exit_sql_budget(
    client: httpx.AsyncClient, container: Container, entry_key: str, exit_key: str
) -> None:
    warm = unique_plate()
    assert (await send_event(client, entry_key, event_body(warm))).status_code == 200
    assert (
        await send_event(client, exit_key, event_body(warm, event_type="EXIT", camera_code="EXIT_01"))
    ).status_code == 200

    plate = unique_plate()
    with sql_log(container) as entry_sql:
        r = await send_event(client, entry_key, event_body(plate))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ENTRY_CONFIRMED"
    with sql_log(container) as exit_sql:
        r = await send_event(client, exit_key, event_body(plate, event_type="EXIT", camera_code="EXIT_01"))
    assert r.status_code == 200, r.text

    assert len(entry_sql) <= ENTRY_BUDGET, "\n".join(entry_sql)
    assert len(exit_sql) <= EXIT_BUDGET, "\n".join(exit_sql)
    # справочники не читаются на горячем пути: площадка/шлагбаум/тариф приходят из кеша процесса
    assert not [s for s in entry_sql + exit_sql if "FROM parking_lot" in s or "FROM barrier b" in s]
    assert not [s for s in exit_sql if "FROM tariff_plan " in s or "FROM holiday" in s]
    # одна транзакция на событие: set_config ровно один раз
    assert sum("set_config" in s for s in entry_sql) == 1
    assert sum("set_config" in s for s in exit_sql) == 1
    # outbox — одним executemany, а не по оператору на событие
    assert sum("INSERT INTO outbox" in s for s in entry_sql) == 1


async def test_reference_caches_do_not_cross_tenants(container: Container) -> None:
    """Ключ кеша содержит tenant_id: площадка A, прочитанная tenant'ом A, не отдаётся из кеша tenant'у B (RLS)."""
    caches = container.reference_caches
    caches.clear()
    async with container.uow(TENANT_A) as uow:
        lot = await SqlLotDirectory(uow.session, cache=caches.lots, tenant_id=TENANT_A).get(LOT_A1)
    assert lot is not None and lot.tenant_id == TENANT_A
    assert caches.lots.get((TENANT_A, LOT_A1)) is not None
    assert caches.lots.get((TENANT_B, LOT_A1)) is None
    async with container.uow(TENANT_B) as uow:
        assert await SqlLotDirectory(uow.session, cache=caches.lots, tenant_id=TENANT_B).get(LOT_A1) is None  # RLS
    assert caches.lots.get((TENANT_B, LOT_A1)) is None  # «нет площадки» не кешируется


async def test_lot_update_is_visible_immediately_in_process(container: Container) -> None:
    """Запись через SqlLotRepository сбрасывает запись кеша этого процесса — новая политика видна следующему событию."""
    caches = container.reference_caches
    caches.clear()
    async with container.uow(TENANT_A) as uow:
        before = await SqlLotDirectory(uow.session, cache=caches.lots, tenant_id=TENANT_A).get(LOT_A1)
    assert before is not None
    assert caches.lots.get((TENANT_A, LOT_A1)) is before
    lot = await container.lots.get(ADMIN_A, LOT_A1)
    new_capacity = lot.capacity + 1
    await container.lots.update(ADMIN_A, LOT_A1, UpdateLotCommand(capacity=new_capacity), reason="cache test")
    assert caches.lots.get((TENANT_A, LOT_A1)) is None
    async with container.uow(TENANT_A) as uow:
        after = await SqlLotDirectory(uow.session, cache=caches.lots, tenant_id=TENANT_A).get(LOT_A1)
    assert after is not None and after.capacity == new_capacity
    await container.lots.update(ADMIN_A, LOT_A1, UpdateLotCommand(capacity=lot.capacity), reason="cache test rollback")
