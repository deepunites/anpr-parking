"""Приоритет решений (I-8): blacklist выигрывает у всего, VIP > абонемент > whitelist (max скидка) > NORMAL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from parking.modules.access.domain import AccessKind, AccessRule, decide
from parking.shared.errors import ValidationError

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
TENANT, LOT, OTHER_LOT = uuid4(), uuid4(), uuid4()
HASH = b"\x01" * 32


def rule(kind: AccessKind, *, lot=None, discount=0, valid_to=None, revoked=None, source=None) -> AccessRule:  # type: ignore[no-untyped-def]
    if kind is AccessKind.VIP or kind is AccessKind.SUBSCRIPTION:
        discount = 100
    return AccessRule(
        id=uuid4(),
        tenant_id=TENANT,
        lot_id=lot,
        kind=kind,
        plate_hash=HASH,
        valid_from=NOW - timedelta(days=1),
        valid_to=valid_to,
        discount_percent=discount,
        source_id=source or (uuid4() if kind is AccessKind.SUBSCRIPTION else None),
        revoked_at=revoked,
    )


def test_blacklist_beats_everything() -> None:
    rules = [
        rule(AccessKind.VIP),
        rule(AccessKind.SUBSCRIPTION),
        rule(AccessKind.WHITELIST, discount=50),
        rule(AccessKind.BLACKLIST),
    ]
    decision = decide(rules, LOT, NOW)
    assert decision.kind is AccessKind.BLACKLIST and not decision.opens_barrier_automatically


def test_priority_chain_and_max_discount() -> None:
    assert decide([rule(AccessKind.SUBSCRIPTION), rule(AccessKind.VIP)], LOT, NOW).kind is AccessKind.VIP
    assert (
        decide([rule(AccessKind.WHITELIST, discount=20), rule(AccessKind.SUBSCRIPTION)], LOT, NOW).kind
        is AccessKind.SUBSCRIPTION
    )
    best = decide([rule(AccessKind.WHITELIST, discount=20), rule(AccessKind.WHITELIST, discount=70)], LOT, NOW)
    assert best.kind is AccessKind.WHITELIST and best.discount_percent == 70
    assert decide([], LOT, NOW).kind is AccessKind.NORMAL


def test_lot_scope_validity_and_revocation() -> None:
    assert decide([rule(AccessKind.BLACKLIST, lot=OTHER_LOT)], LOT, NOW).kind is AccessKind.NORMAL
    assert decide([rule(AccessKind.BLACKLIST, lot=LOT)], LOT, NOW).kind is AccessKind.BLACKLIST
    assert decide([rule(AccessKind.VIP, valid_to=NOW)], LOT, NOW).kind is AccessKind.NORMAL, "valid_to исключён"
    expiring = rule(AccessKind.VIP, valid_to=NOW + timedelta(minutes=1))
    assert decide([expiring], LOT, NOW).valid_until == NOW + timedelta(minutes=1)
    assert decide([rule(AccessKind.BLACKLIST, revoked=NOW - timedelta(seconds=1))], LOT, NOW).kind is AccessKind.NORMAL


def test_rule_invariants() -> None:
    with pytest.raises(ValidationError):
        rule(AccessKind.BLACKLIST, discount=10)
    with pytest.raises(ValidationError):
        rule(AccessKind.WHITELIST, discount=0)
    with pytest.raises(ValidationError):
        AccessRule(
            id=uuid4(),
            tenant_id=TENANT,
            lot_id=None,
            kind=AccessKind.SUBSCRIPTION,
            plate_hash=HASH,
            valid_from=NOW,
            valid_to=None,
            discount_percent=100,
        )
    with pytest.raises(ValidationError):
        rule(AccessKind.NORMAL)


def test_decision_expires_when_a_future_rule_starts() -> None:
    """Машина въехала до старта абонемента и выезжает после: NORMAL действует только до valid_from абонемента,
    иначе кеш решения отдал бы NORMAL на выезде."""
    from dataclasses import replace

    future = replace(rule(AccessKind.SUBSCRIPTION), valid_from=NOW + timedelta(minutes=5))
    decision = decide([future], LOT, NOW)
    assert decision.kind is AccessKind.NORMAL and decision.valid_until == NOW + timedelta(minutes=5)
    later = decide([future], LOT, NOW + timedelta(minutes=6))
    assert later.kind is AccessKind.SUBSCRIPTION and later.valid_until is None
    # выбранное правило кончается раньше, чем начнётся следующее — горизонт = конец выбранного
    current = replace(rule(AccessKind.WHITELIST, discount=10), valid_to=NOW + timedelta(minutes=2))
    both = decide([current, future], LOT, NOW)
    assert both.kind is AccessKind.WHITELIST and both.valid_until == NOW + timedelta(minutes=2)
    # правило другой площадки горизонт не двигает
    other = replace(rule(AccessKind.VIP, lot=OTHER_LOT), valid_from=NOW + timedelta(minutes=1))
    assert decide([other], LOT, NOW).valid_until is None
