"""Версия тарифа выбирается по моменту выезда; новая версия не меняет расчёт прошлых выездов (I-7, I-12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.tariff.application.contracts import AccessKind, AccessTerms
from parking.modules.tariff.application.service import PublishVersionCommand
from parking.runtime.container import Container
from parking.shared.errors import ConflictError, NotFoundError, ValidationError
from parking.shared.money import Money
from tests.conftest import LOT_A2, LOT_B1, PLAN_A1, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer

TZ = ZoneInfo("Asia/Tashkent")
ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
RULES_V2 = {
    "grace_minutes": 0,
    "first_hour_price": 2000000,
    "price_per_hour": 1000000,
    "daily_max": None,
    "rounding_mode": "UP",
    "rounding_step_minutes": 60,
}


async def test_quote_uses_seed_version(container: Container) -> None:
    entry = datetime(2026, 9, 14, 8, tzinfo=TZ)
    calc = await container.tariffs.quote(ADMIN, LOT_A2, entry, entry + timedelta(hours=2), AccessTerms())
    # сид площадки A2: первый час 1 000 000, далее 500 000/ч, шаг 30 UP
    assert calc.amount == Money(1500000) and calc.tariff_plan_version_id is not None and calc.billing_minutes == 120
    with pytest.raises(ValidationError):
        await container.tariffs.quote(ADMIN, LOT_A2, entry, entry, AccessTerms())


async def test_new_version_applies_only_from_its_valid_from(container: Container) -> None:
    plan = await container.tariffs.create_plan(
        ADMIN, LOT_A2, code="RETRO", name="retro", is_default=False, reason="test"
    )
    versions = await container.tariffs.list_versions(ADMIN, plan.id)
    assert versions == []
    plans = await container.tariffs.list_plans(ADMIN, LOT_A2)
    assert any(p.code == "RETRO" for p in plans)
    with pytest.raises(ConflictError):
        await container.tariffs.create_plan(ADMIN, LOT_A2, code="RETRO", name="dup", is_default=False, reason="test")

    # план по умолчанию площадки A2 (сид): публикуем v2 с завтрашнего дня
    default_plan = next(p for p in plans if p.is_default)
    switch_at = datetime.now(tz=UTC) + timedelta(days=1)
    v2 = await container.tariffs.publish_version(
        ADMIN, PublishVersionCommand(plan_id=default_plan.id, rules=RULES_V2, valid_from=switch_at), reason="new prices"
    )
    assert v2.version == 2 and v2.valid_to is None
    listed = await container.tariffs.list_versions(ADMIN, default_plan.id)
    assert listed[0].valid_to == switch_at, "предыдущая открытая версия закрыта в момент начала новой"

    past_entry = datetime.now(tz=UTC) - timedelta(hours=3)
    before = await container.tariffs.quote(ADMIN, LOT_A2, past_entry, past_entry + timedelta(hours=2), AccessTerms())
    assert before.amount == Money(1500000) and before.tariff_plan_version_id == listed[0].id, (
        "прошлое считается по старой версии"
    )
    after = await container.tariffs.quote(ADMIN, LOT_A2, switch_at, switch_at + timedelta(hours=2), AccessTerms())
    assert after.amount == Money(3000000) and after.tariff_plan_version_id == v2.id

    with pytest.raises(ConflictError, match="I-12"):
        await container.tariffs.publish_version(
            ADMIN,
            PublishVersionCommand(
                plan_id=default_plan.id,
                rules=RULES_V2,
                valid_from=switch_at - timedelta(hours=1),
                valid_to=switch_at + timedelta(hours=1),
            ),
            reason="overlap",
        )
    with pytest.raises(ValidationError):
        await container.tariffs.publish_version(
            ADMIN,
            PublishVersionCommand(plan_id=default_plan.id, rules={"price_per_hour": 1.5}, valid_from=switch_at),
            reason="bad",
        )
    with pytest.raises(NotFoundError):
        await container.tariffs.list_versions(ADMIN, PLAN_A1.__class__(int=0))


async def test_quote_endpoint_returns_money_formatted(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    headers = await bearer(client, users[Role.MANAGER])
    response = await client.post(
        "/api/v1/tariff/quote",
        headers=headers,
        json={
            "lot_id": str(LOT_A2),
            "entry": "2026-09-14T08:00:00+05:00",
            "exit": "2026-09-14T10:00:00+05:00",
            "access_kind": AccessKind.WHITELIST.value,
            "discount_percent": 33,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["gross"]["minor"] == 1500000 and body["discount"]["minor"] == 495000 and body["amount"]["minor"] == 1005000
    )
    assert body["amount"]["formatted"] == "10 050,00 сум" and body["breakdown"][0]["kind"] == "FIRST_HOUR"


PREVIEW_RULES = {
    "grace_minutes": 15,
    "first_hour_price": 500_000,  # 5 000 сум
    "price_per_hour": 300_000,  # 3 000 сум/ч
    "rounding_mode": "UP",
    "rounding_step_minutes": 15,
    "daily_max": 3_000_000,  # 30 000 сум в сутки
}


async def test_preview_prices_a_draft_without_publishing(
    client: httpx.AsyncClient, users: dict[Role, Principal], container: Container
) -> None:
    """Экран «Тариф»: черновик считается тем же движком, что и оплата, а версия не создаётся."""
    plans_before = await container.tariffs.list_plans(ADMIN, LOT_A2)
    versions_before = [len(await container.tariffs.list_versions(ADMIN, p.id)) for p in plans_before]
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    response = await client.post(
        "/api/v1/tariff/preview",
        headers=headers,
        json={
            "lot_id": str(LOT_A2),
            "rules": PREVIEW_RULES,
            "start": "2026-09-14T08:00:00+05:00",
            "durations_minutes": [10, 60, 120, 600],
        },
    )
    assert response.status_code == 200, response.text
    amounts = [row["amount"]["minor"] for row in response.json()]
    # 10 мин — бесплатно; час — 5 000; два часа — 5 000 + 3 000; 10 часов — 32 000, но не больше 30 000 в сутки
    assert amounts == [0, 500_000, 800_000, 3_000_000]
    assert response.json()[3]["amount"]["formatted"] == "30 000,00 сум"
    assert [len(await container.tariffs.list_versions(ADMIN, p.id)) for p in plans_before] == versions_before


async def test_preview_rejects_bad_rules_foreign_lots_and_cashiers(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    bad = await client.post(
        "/api/v1/tariff/preview", headers=admin, json={"lot_id": str(LOT_A2), "rules": {"price_per_hour": 1.5}}
    )
    assert bad.status_code == 422
    foreign = await client.post(
        "/api/v1/tariff/preview", headers=admin, json={"lot_id": str(LOT_B1), "rules": PREVIEW_RULES}
    )
    assert foreign.status_code == 404  # площадка другого клиента не видна
    cashier = await bearer(client, users[Role.CASHIER])
    denied = await client.post(
        "/api/v1/tariff/preview", headers=cashier, json={"lot_id": str(LOT_A2), "rules": PREVIEW_RULES}
    )
    assert denied.status_code == 403


async def test_preview_rejects_huge_steps_and_odd_input_with_422(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    """Шаг округления в миллионы минут занимал движок секундами (DoS одного воркера); кривой JSON и год 9999 — 422, не 500."""
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    for rules, start in (
        ({**PREVIEW_RULES, "rounding_step_minutes": 10_000_000}, None),
        ({**PREVIEW_RULES, "grace_minutes": 100_000}, None),
        ({**PREVIEW_RULES, "holiday": 5}, None),
        ({**PREVIEW_RULES, "weekday_overrides": [1]}, None),
        (PREVIEW_RULES, "9999-12-31T23:00:00+05:00"),
        (PREVIEW_RULES, "2026-10-10T10:00:00"),  # без часового пояса
    ):
        body: dict[str, object] = {"lot_id": str(LOT_A2), "rules": rules}
        if start:
            body["start"] = start
        response = await client.post("/api/v1/tariff/preview", headers=admin, json=body)
        assert response.status_code == 422, (rules, start, response.text)
