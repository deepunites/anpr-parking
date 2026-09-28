"""Исполнитель команд: ретраи OPEN (не CLOSE), circuit breaker 5 отказов → OPEN 60 с → HALF_OPEN проба."""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import asyncpg
import httpx
import structlog
from structlog.testing import capture_logs

from parking.modules.devices.application.commands import BarrierCommandExecutor, SqlBarrierCommands
from parking.modules.devices.domain.command import CommandSource, CommandState
from parking.modules.identity.application.contracts import Role
from parking.platform.breaker import BreakerRegistry, BreakerState
from parking.runtime.container import Container, build_device_repos
from tests.conftest import BARRIER_A1_IN, CAM_A1_IN, LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal, bearer
from tests.support.fakes import FakeBarrierDriver, fake_registry, no_sleep


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _request(container: Container, source: CommandSource = CommandSource.SYSTEM) -> UUID:
    async with container.uow(TENANT_A) as uow:
        commands = SqlBarrierCommands(uow, build_device_repos(uow), container.clock)
        result = await commands.request_open(
            lot_id=LOT_A1,
            camera_id=CAM_A1_IN,
            session_id=None,
            saga_id=None,
            source=source,
            idempotency_key=hashlib.sha256(uuid4().bytes).digest(),
        )
    assert result.command_id is not None
    return result.command_id


async def test_open_retried_three_times_then_succeeds(container: Container, owner: asyncpg.Connection) -> None:
    registry, driver = fake_registry(FakeBarrierDriver(results=[False, False, True]))
    executor = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )
    command_id = await _request(container)
    assert await executor.execute(TENANT_A, command_id) is CommandState.OK
    assert [c[0] for c in driver.calls] == ["open", "open", "open"]


async def test_circuit_breaker_opens_after_five_failures_and_probes_after_ttl(container: Container) -> None:
    """Gate: лог переходов breaker вставляется в docs/backend/gate-4.out."""
    clock = FakeMonotonic()
    transitions: list[tuple[str, str]] = []
    breaker_log = structlog.get_logger("devices.barrier")

    def on_change(name: str, old: BreakerState, new: BreakerState) -> None:
        transitions.append((old.value, new.value))
        breaker_log.warning("barrier_breaker_state", barrier_id=name, old=old.value, new=new.value)

    breakers = BreakerRegistry(threshold=5, ttl_seconds=60.0, monotonic=clock, on_state_change=on_change)
    registry, driver = fake_registry(FakeBarrierDriver(results=[False]))
    executor = BarrierCommandExecutor(
        uow_factory=container.uow,
        repos=build_device_repos,
        drivers=registry,
        clock=container.clock,
        breakers=breakers,
        retries=3,
        sleep=no_sleep,
    )
    with capture_logs() as logs:
        first = await executor.execute(TENANT_A, await _request(container))  # 3 отказа
        second = await executor.execute(TENANT_A, await _request(container))  # 4-й, 5-й → OPEN, 3-я попытка отсечена
        third = await executor.execute(TENANT_A, await _request(container))  # breaker OPEN: без обращения к драйверу
    assert (first, second, third) == (CommandState.FAILED, CommandState.FAILED, CommandState.FAILED)
    assert len(driver.calls) == 5
    breaker = breakers.get(str(BARRIER_A1_IN))
    assert breaker.state is BreakerState.OPEN
    assert transitions == [("closed", "open")]
    clock.now += 61  # TTL прошёл → HALF_OPEN, одна проба
    driver._results.clear()
    driver._last = True
    with capture_logs() as probe_logs:
        fourth = await executor.execute(TENANT_A, await _request(container))
    assert fourth is CommandState.OK
    assert len(driver.calls) == 6
    assert transitions == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]
    wanted = {"barrier_command_completed", "barrier_breaker_state", "barrier_attempt_failed", "barrier_circuit_open"}
    events = [log_ for log_ in logs + probe_logs if log_["event"] in wanted]
    print("\nGATE barrier breaker log:")
    for entry in events:
        print("  ", {k: v for k, v in entry.items() if k != "log_level"})
    structlog.reset_defaults()


async def test_close_is_not_retried(container: Container, owner: asyncpg.Connection) -> None:
    registry, driver = fake_registry(FakeBarrierDriver(results=[False, True]))
    executor = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )
    command_id = uuid4()
    await owner.execute(
        """INSERT INTO barrier_command (id, tenant_id, lot_id, barrier_id, kind, source, idempotency_key, correlation_id)
           VALUES ($1, $2, $3, $4, 'CLOSE', 'SYSTEM', $5, $6)""",
        command_id,
        TENANT_A,
        LOT_A1,
        BARRIER_A1_IN,
        hashlib.sha256(command_id.bytes).digest(),
        uuid4(),
    )
    assert await executor.execute(TENANT_A, command_id) is CommandState.FAILED
    assert [c[0] for c in driver.calls] == ["close"]


async def test_manual_open_requires_reason_and_is_audited(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    headers = await bearer(client, users[Role.OPERATOR])
    no_reason = await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/open", json={}, headers=headers)
    assert no_reason.status_code == 422, no_reason.text
    short = await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/open", json={"reason": "no"}, headers=headers)
    assert short.status_code == 422
    ok = await client.post(
        f"/api/v1/barriers/{BARRIER_A1_IN}/open",
        json={"reason": "стрела не открылась, машина у шлагбаума"},
        headers=headers,
    )
    assert ok.status_code == 202, ok.text
    command_id = UUID(ok.json()["command_id"])
    audit = await owner.fetchrow(
        "SELECT action, reason, user_id, payload FROM audit_log WHERE action = 'BARRIER_OPEN_MANUAL' AND payload->>'command_id' = $1",
        str(command_id),
    )
    assert (
        audit is not None and audit["reason"].startswith("стрела") and audit["user_id"] == users[Role.OPERATOR].user_id
    )
    row = await owner.fetchrow(
        "SELECT source::text AS source, issued_by, reason FROM barrier_command WHERE id = $1", command_id
    )
    assert row["source"] == "MANUAL" and row["issued_by"] == users[Role.OPERATOR].user_id
