"""Конкурентные сценарии на реальных транзакциях (фаза 8 §3): параллельное закрытие сессии — потоком камеры
(шаги саги из нескольких потребителей) и оператором одновременно; ровно один эффект, ни одного 500."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import ExitStatus, SagaState
from parking.runtime.container import Container
from parking.shared.errors import ConflictError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.fakes import FakeBarrierDriver
from tests.support.parking_fixtures import entry_cmd, exit_cmd, subscription_decision

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
PARALLEL = 12


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01C{uuid4().int % 900 + 100}CC")


async def _free_exit_saga(container: Container) -> tuple[UUID, UUID]:
    pl = _plate()
    entered = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=2)))
    assert entered.session_id is not None
    result = await container.parking.start_exit(exit_cmd(pl, decision=subscription_decision()))
    assert result.status is ExitStatus.EXIT_CONFIRMED and result.saga_id is not None
    return entered.session_id, result.saga_id


async def _closed_events(owner: asyncpg.Connection, session_id: UUID) -> int:
    return int(
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.session.closed.v1' AND aggregate_id = $1",
            session_id,
        )
    )


async def test_parallel_saga_steps_produce_one_effect_each(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    """Несколько потребителей получили одно и то же событие саги (at-least-once, XAUTOCLAIM) и шагнули
    одновременно: SELECT … FOR UPDATE по саге сериализует шаги, проигравшие видят новое состояние и не повторяют
    эффект — одна льготная оплата, одна команда шлагбаума, одно закрытие сессии, одно событие session.closed."""
    session_id, saga_id = await _free_exit_saga(container)

    states = await asyncio.gather(*(container.parking.advance_saga(TENANT_A, saga_id) for _ in range(PARALLEL)))
    assert set(states) == {SagaState.BARRIER_OPENING}
    assert await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1", session_id) == 1
    assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE saga_id = $1", saga_id) == 1

    command_id = await owner.fetchval("SELECT barrier_command_id FROM exit_saga WHERE id = $1", saga_id)
    results = await asyncio.gather(*(executor[0].execute(TENANT_A, command_id) for _ in range(PARALLEL)))
    assert len(executor[1].calls) == 1, "команда исполнена контроллером один раз, остальные вызовы — идемпотентны"
    assert {r.value for r in results} <= {"OK", "SENT"}

    states = await asyncio.gather(*(container.parking.advance_saga(TENANT_A, saga_id) for _ in range(PARALLEL)))
    assert set(states) == {SagaState.COMPLETED}
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "CLOSED"
    assert await _closed_events(owner, session_id) == 1
    assert await container.occupancy.get(LOT_A1) == 0, "occupancy уменьшен ровно один раз"


async def test_manual_close_races_with_camera_close(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    """Шлагбаум открылся, и в этот момент оператор закрывает сессию вручную: побеждает один, второй получает
    ConflictError либо идемпотентный no-op; сессия закрыта один раз, сага завершена, событий закрытия — одно."""
    session_id, saga_id = await _free_exit_saga(container)
    assert await container.parking.advance_saga(TENANT_A, saga_id) is SagaState.BARRIER_OPENING
    command_id = await owner.fetchval("SELECT barrier_command_id FROM exit_saga WHERE id = $1", saga_id)
    await executor[0].execute(TENANT_A, command_id)

    async def manual() -> str:
        try:
            await container.parking.close_manual(ADMIN, session_id, reason="стрелу подняли ключом")
        except ConflictError:
            return "conflict"
        return "closed"

    async def camera() -> str:
        return (await container.parking.advance_saga(TENANT_A, saga_id)).value

    outcomes = await asyncio.gather(*[manual() if i % 2 else camera() for i in range(PARALLEL)])
    manual_outcomes = [o for i, o in enumerate(outcomes) if i % 2]
    assert manual_outcomes.count("closed") <= 1 and set(manual_outcomes) <= {"closed", "conflict"}
    assert {o for i, o in enumerate(outcomes) if not i % 2} == {"COMPLETED"}

    row = await owner.fetchrow(
        "SELECT status::text AS status, exit_kind::text AS exit_kind FROM parking_session WHERE id = $1", session_id
    )
    assert row is not None and row["status"] == "CLOSED" and row["exit_kind"] in {"CAMERA", "MANUAL"}
    assert await owner.fetchval("SELECT state::text FROM exit_saga WHERE id = $1", saga_id) == "COMPLETED"
    assert await _closed_events(owner, session_id) == 1
    assert await container.occupancy.get(LOT_A1) == 0
