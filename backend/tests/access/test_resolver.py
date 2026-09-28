"""AccessResolver: инвалидация ≤ 5 с при прогретом кеше, деградация без Redis, один запрос в БД, p95 < 10 мс."""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
from sqlalchemy import event

from parking.modules.access.application.admin_service import AddRuleCommand, CreateSubscriptionCommand
from parking.modules.access.application.contracts import AccessKind
from parking.modules.access.application.resolver import CachedAccessResolver, LocalDecisionCache
from parking.modules.access.infrastructure.redis_cache import RedisDecisionCache
from parking.modules.access.infrastructure.repositories import SqlAccessRuleRepository
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.platform.db.engine import Pool
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, LOT_A2, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())


def _hash(container: Container, plate: str) -> bytes:
    return container.plate_crypto.hash(TENANT_A, PlateNumber.parse(plate))


async def test_blacklist_takes_effect_within_declared_window_on_warm_cache(container: Container) -> None:
    plate = "01B111BB"
    h = _hash(container, plate)
    now = datetime.now(tz=UTC)
    assert (await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)).kind is AccessKind.NORMAL
    assert (await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)).kind is AccessKind.NORMAL, (
        "прогрев локального и Redis кеша"
    )
    started = time.perf_counter()
    await container.access.add_rule(
        ADMIN, AddRuleCommand(plate=plate, kind=AccessKind.BLACKLIST, lot_id=None), reason="угон"
    )
    decision = await container.access_resolver.resolve(TENANT_A, LOT_A1, h, datetime.now(tz=UTC))
    elapsed = time.perf_counter() - started
    assert decision.kind is AccessKind.BLACKLIST and not decision.opens_barrier_automatically
    assert elapsed < 5.0, f"вступило в силу за {elapsed:.2f} с (заявлено ≤ 5 с)"
    assert (
        await container.access_resolver.resolve(TENANT_A, LOT_A2, h, datetime.now(tz=UTC))
    ).kind is AccessKind.BLACKLIST, "глобальное правило — все площадки"


async def test_priority_via_admin_service_and_revoke(container: Container, owner: asyncpg.Connection) -> None:
    plate = "01P222PP"
    h = _hash(container, plate)
    await container.access.add_rule(
        ADMIN,
        AddRuleCommand(plate=plate, kind=AccessKind.WHITELIST, lot_id=LOT_A1, discount_percent=30),
        reason="арендатор",
    )
    await container.access.add_rule(
        ADMIN, AddRuleCommand(plate=plate, kind=AccessKind.VIP, lot_id=LOT_A1), reason="директор"
    )
    now = datetime.now(tz=UTC)
    assert (await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)).kind is AccessKind.VIP
    assert (await container.access_resolver.resolve(TENANT_A, LOT_A2, h, now)).kind is AccessKind.NORMAL, (
        "правила только для A1"
    )
    rules = await container.access.list_rules(ADMIN, kind=None, lot_id=LOT_A1, limit=50, after=None)
    mine = [r for r in rules if r.plate == plate]
    assert {r.kind for r in mine} == {AccessKind.WHITELIST, AccessKind.VIP} and all(r.plate == "01P222PP" for r in mine)
    vip = next(r for r in mine if r.kind is AccessKind.VIP)
    await container.access.revoke_rule(ADMIN, vip.id, reason="уволен")
    decision = await container.access_resolver.resolve(TENANT_A, LOT_A1, h, datetime.now(tz=UTC))
    assert decision.kind is AccessKind.WHITELIST and decision.discount_percent == 30
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'ACCESS_RULE_REVOKED' AND entity_id = $1", vip.id
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'access.rule.changed.v1' AND aggregate_id = $1", vip.id
        )
        == 2
    )


async def test_subscription_expiry_is_respected_despite_cache(container: Container) -> None:
    plate = "01S333SS"
    h = _hash(container, plate)
    now = datetime.now(tz=UTC)
    await container.access.create_subscription(
        ADMIN,
        CreateSubscriptionCommand(
            plate=plate,
            lot_id=None,
            plan_code="MONTH",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(seconds=2),
            price_minor=30000000,
        ),
        reason="продажа абонемента",
    )
    first = await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)
    assert first.kind is AccessKind.SUBSCRIPTION and first.source_id is not None and first.valid_until is not None
    later = await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now + timedelta(seconds=3))
    assert later.kind is AccessKind.NORMAL, "кеш не переживает срок действия правила"


async def test_degrades_to_database_when_redis_is_down(container: Container) -> None:
    from redis.asyncio import Redis

    dead = RedisDecisionCache(Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.05, socket_timeout=0.05))
    resolver = CachedAccessResolver(
        uow_factory=container.uow,
        repository=lambda uow: SqlAccessRuleRepository(uow.session),
        cache=dead,
        local=LocalDecisionCache(),
        clock=container.clock,
    )
    plate = "01D444DD"
    await container.access.add_rule(
        ADMIN, AddRuleCommand(plate=plate, kind=AccessKind.BLACKLIST, lot_id=None), reason="test"
    )
    decision = await resolver.resolve(TENANT_A, LOT_A1, _hash(container, plate), datetime.now(tz=UTC))
    assert decision.kind is AccessKind.BLACKLIST
    await dead._r.aclose()


async def test_cold_path_is_exactly_one_select(container: Container) -> None:
    statements: list[str] = []
    engine = container.database.engines[Pool.CRITICAL].sync_engine

    def listener(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement.strip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", listener)
    try:
        await container.access_resolver.resolve_uncached(
            TENANT_A, LOT_A1, _hash(container, "01C555CC"), datetime.now(tz=UTC)
        )
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert statements.count("SELECT") == 2, statements  # set_config(tenant) + один запрос правил
    assert "SELECT" in statements and len([s for s in statements if s not in {"SELECT"}]) == 0


async def test_warm_cache_p95_under_10ms(container: Container) -> None:
    h = _hash(container, "01W666WW")
    now = datetime.now(tz=UTC)
    await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)
    redis_path: list[float] = []
    local_path: list[float] = []
    for _ in range(200):
        container.access_resolver._local.invalidate(TENANT_A, h)
        started = time.perf_counter()
        await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)
        redis_path.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        await container.access_resolver.resolve(TENANT_A, LOT_A1, h, now)
        local_path.append((time.perf_counter() - started) * 1000)
    p95_redis = statistics.quantiles(redis_path, n=20)[18]
    p95_local = statistics.quantiles(local_path, n=20)[18]
    print(f"\naccess resolve p95: redis path {p95_redis:.2f} ms, local path {p95_local:.3f} ms")
    assert p95_redis < 10.0 and p95_local < 1.0


async def test_resolve_endpoint_masks_plate(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    headers = await bearer(client, users[Role.OPERATOR])
    response = await client.get(
        "/api/v1/access/resolve", params={"lot_id": str(LOT_A1), "plate": "01 A 999 AA"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "NORMAL" and body["plate"] == "01A***AA" and body["opens_barrier_automatically"] is True
    bad = await client.get("/api/v1/access/resolve", params={"lot_id": str(LOT_A1), "plate": "Ж123"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_PLATE"
