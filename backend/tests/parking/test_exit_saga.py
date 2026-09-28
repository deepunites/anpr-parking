"""EXIT-сага (3.5): шаги — отдельные транзакции, возобновление после «kill -9» в каждом промежуточном состоянии,
ровно один эффект на шаг, компенсации (EXPIRED, MANUAL_REVIEW), ADR-12."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from parking.modules.access.application.contracts import AccessDecision, AccessKind
from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import ExitStatus, SagaState
from parking.modules.tenancy.application.contracts import LotPolicies, UnknownExitPolicy
from parking.modules.tenancy.application.lot_service import UpdateLotCommand
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.fakes import FakeBarrierDriver, no_sleep
from tests.support.parking_fixtures import entry_cmd, exit_cmd, subscription_decision

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01E{uuid4().int % 900 + 100}EE")


async def _enter(container: Container, pl: PlateNumber, hours_ago: float = 2.0) -> UUID:
    result = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=hours_ago)))
    assert result.session_id is not None
    return result.session_id


async def _confirm_cash(owner: asyncpg.Connection, payment_id: UUID) -> None:
    """Подтверждение наличных — API фазы 5; здесь тот же UPDATE, что сделает кассирский сервис."""
    await owner.execute(
        "UPDATE payment SET status = 'PAID', paid_at = now(), method = 'CASH', operator_id = $2, shift_id = $3 WHERE id = $1",
        payment_id,
        UUID("00000000-0000-4000-8000-0000000a0003"),
        UUID("00000000-0000-4000-8000-000000a50001"),
    )


async def _run_barrier(container: Container, executor: BarrierCommandExecutor, saga_id: UUID) -> None:
    """Имитирует потребителя: исполняет команду шлагбаума саги и продвигает сагу."""
    saga = await container.parking.get_saga_by_id(TENANT_A, saga_id)
    assert saga.barrier_command_id is not None
    await executor.execute(TENANT_A, saga.barrier_command_id)
    await container.parking.advance_saga(TENANT_A, saga_id)


async def test_subscription_exit_full_flow(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    pl = _plate()
    session_id = await _enter(container, pl)
    result = await container.parking.start_exit(exit_cmd(pl, decision=subscription_decision()))
    assert result.status is ExitStatus.EXIT_CONFIRMED and result.amount_minor == 0 and result.saga_id is not None
    # шаг 1: ACCESS_RESOLVED → PAYMENT_SETTLED → BARRIER_OPENING (в одной транзакции: льгота + команда)
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.BARRIER_OPENING
    payments = await owner.fetch(
        "SELECT status::text AS status, amount, method::text AS method FROM payment WHERE session_id = $1", session_id
    )
    assert [(p["status"], p["amount"], p["method"]) for p in payments] == [("PAID", 0, "SUBSCRIPTION")], (
        "льгота видна в отчётах"
    )
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.BARRIER_OPENING, (
        "повторный вызов — без второго эффекта"
    )
    assert await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1", session_id) == 1
    # шаг 2: исполнение команды (вне транзакции) и закрытие
    await _run_barrier(container, executor[0], result.saga_id)
    session = await owner.fetchrow(
        "SELECT status::text AS status, exit_time, billing_minutes, exit_kind::text AS exit_kind FROM parking_session WHERE id = $1",
        session_id,
    )
    assert (
        session is not None
        and session["status"] == "CLOSED"
        and session["exit_kind"] == "CAMERA"
        and session["billing_minutes"] == 0
    )
    saga = await owner.fetchrow(
        "SELECT state::text AS state, barrier_result FROM exit_saga WHERE id = $1", result.saga_id
    )
    assert saga is not None and saga["state"] == "COMPLETED" and saga["barrier_result"] == "OK"
    assert executor[1].calls and executor[1].calls[0][0] == "open"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.session.closed.v1' AND aggregate_id = $1",
            session_id,
        )
        == 1
    )
    assert await container.occupancy.get(LOT_A1) == 0


async def test_paid_exit_waits_for_payment_then_opens(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    from parking.modules.tariff.application.contracts import AccessTerms

    pl = _plate()
    entry_at = datetime.now(tz=UTC) - timedelta(hours=2)
    exit_at = datetime.now(tz=UTC)
    session_id = (await container.parking.process_entry(entry_cmd(pl, at=entry_at))).session_id
    assert session_id is not None
    expected = await container.tariffs.quote(ADMIN, LOT_A1, entry_at, exit_at, AccessTerms())
    result = await container.parking.start_exit(exit_cmd(pl, at=exit_at))
    assert result.status is ExitStatus.PAYMENT_REQUIRED and result.amount_minor == expected.amount.minor > 0
    saga_id = result.saga_id
    assert saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, saga_id) is SagaState.PAYMENT_REQUIRED
    payment = await owner.fetchrow(
        "SELECT id, status::text AS status, amount, method FROM payment WHERE session_id = $1", session_id
    )
    assert (
        payment is not None
        and payment["status"] == "PENDING"
        and payment["amount"] == expected.amount.minor
        and payment["method"] is None
    )
    assert await container.parking.advance_saga(TENANT_A, saga_id) is SagaState.PAYMENT_REQUIRED, "без оплаты сага ждёт"
    assert await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1", session_id) == 1
    # кассир подтвердил оплату (фаза 5 — через API; здесь напрямую) → событие payment.confirmed продвигает сагу
    await _confirm_cash(owner, payment["id"])
    assert await container.parking.advance_saga(TENANT_A, saga_id) is SagaState.BARRIER_OPENING
    await _run_barrier(container, executor[0], saga_id)
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "CLOSED"
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", payment["id"]) == "PAID", (
        "платёж не тронут"
    )


async def test_second_exit_event_joins_active_saga(container: Container, clean_lot: None) -> None:
    pl = _plate()
    await _enter(container, pl)
    first = await container.parking.start_exit(exit_cmd(pl))
    second = await container.parking.start_exit(exit_cmd(pl, camera=None))
    assert second.status is ExitStatus.EXIT_IN_PROGRESS and second.saga_id == first.saga_id
    saga = await container.parking.get_saga_by_id(TENANT_A, first.saga_id)  # type: ignore[arg-type]
    assert len(saga.calculation) > 0 and saga.state is SagaState.ACCESS_RESOLVED


async def test_saga_expires_without_payment_and_session_stays_active(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    pl = _plate()
    session_id = await _enter(container, pl)
    result = await container.parking.start_exit(exit_cmd(pl))
    await container.parking.advance_saga(TENANT_A, result.saga_id)  # type: ignore[arg-type]
    await owner.execute(
        "UPDATE exit_saga SET saga_deadline = now() - interval '1 minute' WHERE id = $1", result.saga_id
    )
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.EXPIRED  # type: ignore[arg-type]
    assert await owner.fetchval("SELECT status::text FROM payment WHERE session_id = $1", session_id) == "CANCELLED"
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "ACTIVE", (
        "следующий EXIT начнёт новую сагу"
    )
    again = await container.parking.start_exit(exit_cmd(pl))
    assert again.status is ExitStatus.PAYMENT_REQUIRED and again.saga_id != result.saga_id


async def test_barrier_failure_after_payment_goes_to_manual_review_and_keeps_payment(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    from tests.support.fakes import fake_registry

    registry, _ = fake_registry(FakeBarrierDriver(results=[False]))
    from parking.runtime.container import build_device_repos

    failing = BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    )
    pl = _plate()
    session_id = await _enter(container, pl)
    result = await container.parking.start_exit(exit_cmd(pl, decision=subscription_decision()))
    await container.parking.advance_saga(TENANT_A, result.saga_id)  # type: ignore[arg-type]
    saga = await container.parking.get_saga_by_id(TENANT_A, result.saga_id)  # type: ignore[arg-type]
    assert saga.barrier_command_id is not None
    await failing.execute(TENANT_A, saga.barrier_command_id)
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.MANUAL_REVIEW  # type: ignore[arg-type]
    row = await owner.fetchrow(
        "SELECT status::text AS status, review_reason FROM parking_session WHERE id = $1", session_id
    )
    assert row is not None and row["status"] == "MANUAL_REVIEW" and row["review_reason"] == "EXIT_BARRIER_FAILED"
    assert await owner.fetchval("SELECT status::text FROM payment WHERE session_id = $1", session_id) == "PAID", (
        "ADR-12: деньги не трогаем"
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.session.manual_review.v1' AND aggregate_id = $1",
            session_id,
        )
        == 1
    )
    # оператор открывает вручную (I-15: reason) → команда MANUAL → сага закрывает сессию
    view = await container.parking.manual_open_exit(ADMIN, session_id, reason="стрела не поднялась, открыл вручную")
    manual_command = UUID(str(view.calculation["manual_command_id"]))
    await BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=fake_registry()[0], clock=container.clock
    ).execute(TENANT_A, manual_command)
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.COMPLETED  # type: ignore[arg-type]
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "CLOSED"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'BARRIER_OPEN_MANUAL' AND session_id = $1", session_id
        )
        == 1
    )


async def test_late_barrier_success_after_review_closes_session_as_camera_exit(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    """Команда шлагбаума застряла в очереди дольше дедлайна шага (30 с) → сага ушла в MANUAL_REVIEW с
    «barrier command PENDING»; затем команда всё же исполнилась и стрела поднялась — машина уехала. Сессия должна
    закрыться как выезд по камере без участия оператора (нагрузочный прогон фазы 8: 469 таких саг за 5 минут)."""
    pl = _plate()
    session_id = await _enter(container, pl)
    result = await container.parking.start_exit(exit_cmd(pl, decision=subscription_decision()))
    assert result.saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.BARRIER_OPENING
    await owner.execute(
        "UPDATE exit_saga SET step_deadline = now() - interval '1 second' WHERE id = $1", result.saga_id
    )
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.MANUAL_REVIEW
    review = await owner.fetchrow("SELECT review_reason, last_error FROM exit_saga WHERE id = $1", result.saga_id)
    assert review is not None and review["review_reason"] == "EXIT_BARRIER_FAILED"
    assert review["last_error"] == "barrier command PENDING"
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "MANUAL_REVIEW"

    await _run_barrier(container, executor[0], result.saga_id)  # опоздавшая команда исполнена: OK → сага шагает
    row = await owner.fetchrow(
        "SELECT status::text AS status, exit_kind::text AS exit_kind FROM parking_session WHERE id = $1", session_id
    )
    assert row is not None and row["status"] == "CLOSED" and row["exit_kind"] == "CAMERA"
    saga = await owner.fetchrow(
        "SELECT state::text AS state, barrier_result FROM exit_saga WHERE id = $1", result.saga_id
    )
    assert saga is not None and saga["state"] == "COMPLETED" and saga["barrier_result"] == "OK_LATE"
    assert await container.occupancy.get(LOT_A1) == 0


@pytest.mark.parametrize(
    "crash_after", ["ACCESS_RESOLVED", "PAYMENT_REQUIRED", "PAYMENT_SETTLED", "BARRIER_OPENING", "SESSION_CLOSED"]
)
async def test_saga_resumes_after_crash_in_every_intermediate_state(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
    crash_after: str,
) -> None:
    """«kill -9» = состояние в БД записано, следующий шаг не выполнен. Recovery-сканер подбирает по истёкшему step_deadline."""
    pl = _plate()
    session_id = await _enter(container, pl)
    result = await container.parking.start_exit(exit_cmd(pl))
    saga_id = result.saga_id
    assert saga_id is not None
    if crash_after != "ACCESS_RESOLVED":
        await container.parking.advance_saga(TENANT_A, saga_id)  # → PAYMENT_REQUIRED
        payment_id = await owner.fetchval("SELECT id FROM payment WHERE session_id = $1", session_id)
        await _confirm_cash(owner, payment_id)
    if crash_after in {"PAYMENT_SETTLED", "BARRIER_OPENING", "SESSION_CLOSED"}:
        await owner.execute(
            "UPDATE exit_saga SET state = 'PAYMENT_SETTLED' WHERE id = $1", saga_id
        ) if crash_after == "PAYMENT_SETTLED" else None
    if crash_after in {"BARRIER_OPENING", "SESSION_CLOSED"}:
        await container.parking.advance_saga(TENANT_A, saga_id)  # PAYMENT_REQUIRED → BARRIER_OPENING (команда записана)
        command_id = await owner.fetchval("SELECT barrier_command_id FROM exit_saga WHERE id = $1", saga_id)
        await executor[0].execute(TENANT_A, command_id)  # контроллер ответил, событие command_completed «потерялось»
    if crash_after == "SESSION_CLOSED":
        await owner.execute(
            "UPDATE exit_saga SET state = 'SESSION_CLOSED', barrier_result = 'OK' WHERE id = $1", saga_id
        )
    # «падение»: шаг не сделан, дедлайн шага прошёл
    await owner.execute("UPDATE exit_saga SET step_deadline = now() - interval '1 minute' WHERE id = $1", saga_id)
    recovered = await container.parking.recover_due(container.saga_scanner)
    assert recovered >= 1
    state = await owner.fetchval("SELECT state::text FROM exit_saga WHERE id = $1", saga_id)
    if crash_after == "ACCESS_RESOLVED":
        assert state == "PAYMENT_REQUIRED"
    elif crash_after == "PAYMENT_REQUIRED":
        assert state == "BARRIER_OPENING"
        await _run_barrier(container, executor[0], saga_id)
        assert await owner.fetchval("SELECT state::text FROM exit_saga WHERE id = $1", saga_id) == "COMPLETED"
    elif crash_after == "PAYMENT_SETTLED":
        assert state == "BARRIER_OPENING"
    else:
        assert state == "COMPLETED"
        assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "CLOSED"
    assert await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1", session_id) == 1, (
        "ровно одно обязательство"
    )
    assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE saga_id = $1", saga_id) <= 1, (
        "ровно одна команда"
    )


async def test_exit_without_session_policies(container: Container, owner: asyncpg.Connection, clean_lot: None) -> None:
    unknown = await container.parking.start_exit(exit_cmd(_plate()))
    assert unknown.status is ExitStatus.EXIT_WITHOUT_SESSION and unknown.session_id is not None
    row = await owner.fetchrow(
        "SELECT status::text AS status, review_reason, entry_unknown FROM parking_session WHERE id = $1",
        unknown.session_id,
    )
    assert (
        row is not None
        and row["status"] == "MANUAL_REVIEW"
        and row["review_reason"] == "EXIT_UNKNOWN_SESSION"
        and row["entry_unknown"]
    )
    await container.lots.update(
        ADMIN,
        LOT_A1,
        UpdateLotCommand(policies=LotPolicies(unknown_exit_policy=UnknownExitPolicy.OPEN_FREE)),
        reason="окно запуска",
    )
    try:
        free = await container.parking.start_exit(exit_cmd(_plate()))
        assert free.status is ExitStatus.EXIT_CONFIRMED
        closed = await owner.fetchrow(
            "SELECT status::text AS status, billing_minutes, closed_reason FROM parking_session WHERE id = $1",
            free.session_id,
        )
        assert (
            closed is not None
            and closed["status"] == "CLOSED"
            and closed["billing_minutes"] == 0
            and closed["closed_reason"] == "UNKNOWN_EXIT_OPEN_FREE"
        )
        assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", free.session_id) == 1
    finally:
        await container.lots.update(ADMIN, LOT_A1, UpdateLotCommand(policies=LotPolicies()), reason="cleanup")


async def test_prepaid_session_exits_without_new_obligation(
    container: Container,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    owner: asyncpg.Connection,
    clean_lot: None,
) -> None:
    pl = _plate()
    session_id = await _enter(container, pl)
    await owner.execute(
        """
        INSERT INTO payment (tenant_id, lot_id, session_id, amount, status, method, operator_id, shift_id, calculation_snapshot, idempotency_key, paid_at, paid_until)
        VALUES ($1, $2, $3, 500000, 'PAID', 'CASH', $4, '00000000-0000-4000-8000-000000a50001', '{}', $5, now(), now() + interval '15 minutes')
        """,
        TENANT_A,
        LOT_A1,
        session_id,
        UUID("00000000-0000-4000-8000-0000000a0003"),
        hashlib.sha256(session_id.bytes + b"prepay").digest(),
    )
    result = await container.parking.start_exit(exit_cmd(pl))
    assert result.status is ExitStatus.EXIT_CONFIRMED and result.amount_minor == 0
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.BARRIER_OPENING  # type: ignore[arg-type]
    assert await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1", session_id) == 1, (
        "нового обязательства нет"
    )
    await _run_barrier(container, executor[0], result.saga_id)  # type: ignore[arg-type]
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "CLOSED"


async def test_low_confidence_and_blacklist_on_exit(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    pl = _plate()
    session_id = await _enter(container, pl)
    review = await container.parking.start_exit(exit_cmd(pl, requires_review=True))
    assert review.status is ExitStatus.MANUAL_REVIEW
    assert (
        await owner.fetchval("SELECT review_reason FROM parking_session WHERE id = $1", session_id)
        == "EXIT_LOW_CONFIDENCE"
    )
    assert await container.occupancy.get(LOT_A1) == 1, "машина всё ещё на площадке"
    pl2 = _plate()
    sid2 = await _enter(container, pl2)
    black = await container.parking.start_exit(exit_cmd(pl2, decision=AccessDecision(kind=AccessKind.BLACKLIST)))
    assert (
        black.status is ExitStatus.MANUAL_REVIEW
        and await owner.fetchval("SELECT review_reason FROM parking_session WHERE id = $1", sid2) == "EXIT_BLACKLIST"
    )


@pytest.mark.parametrize("kind", [AccessKind.VIP, AccessKind.WHITELIST])
async def test_rule_based_privilege_without_subscription_settles_with_rule_as_source(
    container: Container,
    owner: asyncpg.Connection,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    clean_lot: None,
    kind: AccessKind,
) -> None:
    """VIP/whitelist из правила доступа (не абонемента): source_id пуст, источник льготы — само правило
    (ck_privilege_source). До фазы 8 шаг саги падал IntegrityError и сага зависала в ACCESS_RESOLVED."""
    rule_id = uuid4()
    decision = AccessDecision(kind=kind, discount_percent=100, rule_id=rule_id, source_id=None)
    pl = PlateNumber.parse(f"01V{uuid4().int % 900 + 100}VV")
    await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=1)))
    result = await container.parking.start_exit(exit_cmd(pl, decision=decision))
    assert result.status is ExitStatus.EXIT_CONFIRMED and result.saga_id is not None
    assert await container.parking.advance_saga(TENANT_A, result.saga_id) is SagaState.BARRIER_OPENING
    row = await owner.fetchrow(
        "SELECT method::text AS method, source_id, amount, status::text AS status FROM payment WHERE saga_id = $1",
        result.saga_id,
    )
    assert row is not None and row["method"] == kind.value and row["source_id"] == rule_id
    assert row["amount"] == 0 and row["status"] == "PAID"
