"""Лазейка бесплатных минут (MediaPark, 2026-10-19): выезжали на 20-й минуте и въезжали снова, чтобы не платить.
Бесплатные минуты — только первым free_visits въездам машины за 24 часа; дальше сразу фиксированная плата.
Визиты короче минуты (эхо въезда, ошибки камер) не считаются; VIP и абонемент правило не трогает."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.tariff.domain.rules import TariffRules
from parking.runtime.container import Container
from parking.shared.money import Money
from parking.shared.plate import PlateNumber
from tests.conftest import TENANT_A, USER_A_ADMIN
from tests.support.parking_fixtures import entry_cmd, exit_cmd, subscription_decision

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
REPEAT = Money(800000)


@pytest.fixture
def repeat_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тариф площадки + «2 бесплатных въезда за 24 часа, дальше 8 000 сразу» — не трогая версии тарифа в общей БД."""
    original = TariffRules.from_json

    def with_repeat(raw: dict[str, Any]) -> TariffRules:
        return dataclasses.replace(original(raw), free_visits=2, repeat_visit_price=REPEAT)

    monkeypatch.setattr(TariffRules, "from_json", staticmethod(with_repeat))


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01V{uuid4().int % 900 + 100}VV")


async def _past_visit(
    container: Container, owner: asyncpg.Connection, pl: PlateNumber, at: datetime, stay: timedelta
) -> UUID:
    entered = await container.parking.process_entry(entry_cmd(pl, at=at))
    assert entered.session_id is not None
    await owner.execute(
        "UPDATE parking_session SET status = 'CLOSED', exit_time = entry_time + $2::interval, billing_minutes = 0, "
        "exit_kind = 'CAMERA', version = version + 1 WHERE id = $1",
        entered.session_id,
        stay,
    )
    return entered.session_id


async def test_third_visit_in_24_hours_pays_at_once(
    container: Container, owner: asyncpg.Connection, clean_lot: None, repeat_rule: None
) -> None:
    pl = _plate()
    now = datetime.now(tz=UTC)
    await _past_visit(container, owner, pl, now - timedelta(hours=27), timedelta(minutes=10))  # старше суток
    await _past_visit(container, owner, pl, now - timedelta(hours=5), timedelta(minutes=10))
    await _past_visit(container, owner, pl, now - timedelta(hours=3), timedelta(seconds=30))  # эхо — не визит
    second = await container.parking.process_entry(entry_cmd(pl, at=now - timedelta(hours=2)))
    assert second.session_id is not None
    free = await container.parking.start_exit(exit_cmd(pl, at=now - timedelta(hours=2) + timedelta(minutes=10)))
    assert free.amount_minor == 0, "второй въезд за сутки — ещё с бесплатными минутами"
    await owner.execute(
        "UPDATE parking_session SET status = 'CLOSED', exit_time = entry_time + interval '10 minutes', "
        "billing_minutes = 0, exit_kind = 'CAMERA', version = version + 1 WHERE id = $1",
        second.session_id,
    )
    await owner.execute("UPDATE exit_saga SET state = 'COMPLETED' WHERE session_id = $1", second.session_id)

    third = await container.parking.process_entry(entry_cmd(pl, at=now - timedelta(minutes=10)))
    assert third.session_id is not None
    quote = await container.parking.prepay_quote(TENANT_A, third.session_id, now)
    assert quote.amount_minor == REPEAT.minor and "REPEAT_VISIT" in quote.snapshot["applied_rules"]
    exited = await container.parking.start_exit(exit_cmd(pl, at=now))

    assert exited.amount_minor == REPEAT.minor, "третий въезд за 24 часа — сразу 8 000, без бесплатных минут"


async def test_privileged_cars_are_not_limited(
    container: Container, owner: asyncpg.Connection, clean_lot: None, repeat_rule: None
) -> None:
    pl = _plate()
    now = datetime.now(tz=UTC)
    for hours in (6, 4):
        await _past_visit(container, owner, pl, now - timedelta(hours=hours), timedelta(minutes=10))
    entered = await container.parking.process_entry(entry_cmd(pl, at=now - timedelta(minutes=10)))
    assert entered.session_id is not None
    exited = await container.parking.start_exit(exit_cmd(pl, at=now, decision=subscription_decision()))
    assert exited.amount_minor == 0, "абонемент — бесплатно, сколько бы въездов ни было"
