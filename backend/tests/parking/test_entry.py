"""ENTRY (3.5): одна транзакция, I-1 уникальным индексом, гонка 50 параллельных → одна сессия, шлагбаум после коммита."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from parking.modules.access.application.contracts import AccessDecision, AccessKind
from parking.modules.parking.application.contracts import EntryStatus
from parking.runtime.container import Container
from parking.shared.errors import ConflictError
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, LOT_A2, TENANT_A, USER_A_ADMIN
from tests.support.parking_fixtures import entry_cmd


async def test_fifty_parallel_entries_create_one_session(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    pl = PlateNumber.parse(f"01P{uuid4().int % 900 + 100}PP")
    at = datetime.now(tz=UTC)
    results = await asyncio.gather(*(container.parking.process_entry(entry_cmd(pl, at=at)) for _ in range(50)))
    session_ids = {r.session_id for r in results}
    assert len(session_ids) == 1 and all(r.status is EntryStatus.ENTRY_CONFIRMED for r in results)
    assert sum(1 for r in results if not r.duplicate) == 1, (
        "ровно один INSERT победил, остальные вернули существующую сессию"
    )
    session_id = session_ids.pop()
    assert await owner.fetchval("SELECT count(*) FROM parking_session WHERE id = $1", session_id) == 1
    commands = await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", session_id)
    assert commands == 1, "команда шлагбауму создана один раз (idempotency_key)"
    events = await owner.fetch(
        "SELECT event_type FROM outbox WHERE aggregate_id = $1 OR aggregate_id = (SELECT id FROM barrier_command WHERE session_id = $1)",
        session_id,
    )
    assert {e["event_type"] for e in events} == {"parking.session.created.v1", "devices.barrier.command_requested.v1"}
    assert await container.occupancy.get(LOT_A1) == 1


async def test_reentry_after_window_orphans_previous_session(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    pl = PlateNumber.parse("01O777OO")
    first = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=3)))
    within = await container.parking.process_entry(
        entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=3, minutes=-2))
    )
    assert within.duplicate and within.session_id == first.session_id, "повтор в окне reentry_window — та же сессия"
    second = await container.parking.process_entry(entry_cmd(pl))
    assert (
        not second.duplicate
        and second.session_id != first.session_id
        and second.orphaned_session_id == first.session_id
    )
    statuses = await owner.fetch(
        "SELECT id, status::text AS status, closed_reason FROM parking_session WHERE id = ANY($1)",
        [first.session_id, second.session_id],
    )
    by_id = {r["id"]: r for r in statuses}
    assert by_id[first.session_id]["status"] == "ORPHAN" and by_id[first.session_id]["closed_reason"] == "REENTRY"
    assert by_id[second.session_id]["status"] == "ACTIVE"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.session.orphaned.v1' AND aggregate_id = $1",
            first.session_id,
        )
        == 1
    )
    assert await container.occupancy.get(LOT_A1) == 1


async def test_blacklist_never_opens_automatically(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    decision = AccessDecision(kind=AccessKind.BLACKLIST, rule_id=uuid4())
    result = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01B666BB"), decision=decision))
    assert result.status is EntryStatus.ENTRY_DENIED and result.barrier_command_id is None
    row = await owner.fetchrow(
        "SELECT status::text AS status, review_reason FROM parking_session WHERE id = $1", result.session_id
    )
    assert row is not None and row["status"] == "MANUAL_REVIEW" and row["review_reason"] == "ENTRY_BLACKLIST"
    assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", result.session_id) == 0
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'parking.session.manual_review.v1' AND aggregate_id = $1",
            result.session_id,
        )
        == 1
    )


async def test_low_confidence_goes_to_review_without_barrier(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    result = await container.parking.process_entry(
        entry_cmd(PlateNumber.parse("01L555LL"), requires_review=True, confidence=65)
    )
    assert result.status is EntryStatus.MANUAL_REVIEW and result.barrier_command_id is None
    assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", result.session_id) == 0
    assert await container.occupancy.get(LOT_A1) == 0, "машина у стрелы, место не занято"


async def test_full_lot_denies_entry_when_policy_says_so(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    from parking.modules.identity.application.contracts import CurrentUser, Role
    from parking.modules.tenancy.application.contracts import LotPolicies
    from parking.modules.tenancy.application.lot_service import UpdateLotCommand

    admin = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
    await container.lots.update(
        admin, LOT_A1, UpdateLotCommand(policies=LotPolicies(deny_entry_when_full=True)), reason="test"
    )
    try:
        await container.occupancy.set(LOT_A1, 800)
        result = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01F444FF")))
        assert result.status is EntryStatus.ENTRY_DENIED_FULL and result.session_id is None
    finally:
        await container.lots.update(admin, LOT_A1, UpdateLotCommand(policies=LotPolicies()), reason="test cleanup")
        await container.occupancy.set(LOT_A1, 0)


async def test_late_entry_creates_session_without_barrier(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    result = await container.parking.process_entry(
        entry_cmd(PlateNumber.parse("01Z999ZZ"), late=True, at=datetime.now(tz=UTC) - timedelta(hours=5))
    )
    assert result.status is EntryStatus.LATE_ACCEPTED and result.barrier_command_id is None
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", result.session_id) == "ACTIVE"


async def test_unknown_lot_is_rejected(container: Container) -> None:
    from dataclasses import replace

    from parking.shared.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await container.parking.process_entry(replace(entry_cmd(PlateNumber.parse("01N111NN")), lot_id=uuid4()))
    assert LOT_A2 != LOT_A1 and ConflictError
