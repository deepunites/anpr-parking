"""I-8. При активном попадании в blacklist шлагбаум не открывается автоматически.

Механизм: явная ветка в решении доступа (AccessKind.BLACKLIST) → ENTRY_DENIED / EXIT → MANUAL_REVIEW без команды
шлагбауму; решение доступа считается до транзакции и хранится в сессии; открыть может только оператор с reason.
При нарушении: угнанная/запрещённая машина въезжает автоматически, blacklist — бумажка.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg

from parking.modules.access.application.contracts import AccessDecision, AccessKind
from parking.modules.parking.application.contracts import EntryStatus, ExitStatus
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.support.parking_fixtures import entry_cmd, exit_cmd


def _blacklist() -> AccessDecision:
    return AccessDecision(
        kind=AccessKind.BLACKLIST,
        discount_percent=0,
        source_id=uuid4(),
        rule_id=uuid4(),
        valid_until=datetime.now(tz=UTC) + timedelta(days=365),
    )


async def test_entry_with_blacklist_creates_no_barrier_command(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    result = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01B111BB"), decision=_blacklist()))
    assert result.status is EntryStatus.ENTRY_DENIED and result.barrier_command_id is None
    assert result.session_id is not None
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", result.session_id) == (
        "MANUAL_REVIEW"
    )
    assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", result.session_id) == 0


async def test_exit_with_blacklist_goes_to_manual_review_without_opening(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    pl = PlateNumber.parse("01B222BB")
    entry = await container.parking.process_entry(entry_cmd(pl, at=datetime.now(tz=UTC) - timedelta(hours=1)))
    assert entry.status is EntryStatus.ENTRY_CONFIRMED
    before = await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", entry.session_id)
    result = await container.parking.start_exit(exit_cmd(pl, decision=_blacklist()))
    assert result.status is ExitStatus.MANUAL_REVIEW
    after = await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", entry.session_id)
    assert after == before, "на выезде по blacklist новой команды шлагбауму нет"
    saga = await owner.fetchrow("SELECT state::text AS state FROM exit_saga WHERE session_id = $1", entry.session_id)
    assert saga is None or saga["state"] == "MANUAL_REVIEW"


def test_blacklist_wins_over_subscription_and_whitelist() -> None:
    """Приоритет решений: blacklist перекрывает любую льготу той же машины (чистая функция decide)."""
    from parking.modules.access.domain import decide
    from tests.access.test_decide import LOT, NOW, rule

    rules = [
        rule(AccessKind.SUBSCRIPTION),
        rule(AccessKind.WHITELIST, discount=100),
        rule(AccessKind.VIP),
        rule(AccessKind.BLACKLIST),
    ]
    assert decide(rules, LOT, NOW).kind is AccessKind.BLACKLIST
    assert decide(list(reversed(rules)), LOT, NOW).kind is AccessKind.BLACKLIST, "не зависит от порядка правил"
