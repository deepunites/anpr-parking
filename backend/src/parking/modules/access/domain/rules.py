"""Правила доступа и решение по ним. Приоритет: BLACKLIST всегда побеждает (I-8)."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from parking.shared.errors import ValidationError


class AccessKind(enum.StrEnum):
    NORMAL = "NORMAL"
    BLACKLIST = "BLACKLIST"
    WHITELIST = "WHITELIST"
    VIP = "VIP"
    SUBSCRIPTION = "SUBSCRIPTION"


PRIORITY = {AccessKind.BLACKLIST: 0, AccessKind.VIP: 1, AccessKind.SUBSCRIPTION: 2, AccessKind.WHITELIST: 3}


@dataclass(frozen=True, slots=True)
class AccessRule:
    id: UUID
    tenant_id: UUID
    lot_id: UUID | None  # None — всё пространство tenant
    kind: AccessKind
    plate_hash: bytes
    valid_from: datetime
    valid_to: datetime | None
    discount_percent: int = 0
    source_id: UUID | None = None
    reason: str | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is AccessKind.NORMAL:
            raise ValidationError("NORMAL is a decision, not a rule")
        if self.kind is AccessKind.BLACKLIST and self.discount_percent != 0:
            raise ValidationError("blacklist rule has no discount")
        if self.kind in (AccessKind.VIP, AccessKind.SUBSCRIPTION) and self.discount_percent != 100:
            raise ValidationError(f"{self.kind.value} rule must have discount 100")
        if self.kind is AccessKind.WHITELIST and not 1 <= self.discount_percent <= 100:
            raise ValidationError("whitelist discount must be within 1..100")
        if self.kind is AccessKind.SUBSCRIPTION and self.source_id is None:
            raise ValidationError("subscription rule requires source_id")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValidationError("valid_to must be after valid_from")

    def applies(self, lot_id: UUID, at: datetime) -> bool:
        if self.revoked_at is not None and self.revoked_at <= at:
            return False
        if self.lot_id is not None and self.lot_id != lot_id:
            return False
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)


@dataclass(frozen=True, slots=True)
class AccessDecision:
    kind: AccessKind
    discount_percent: int = 0
    source_id: UUID | None = None
    rule_id: UUID | None = None
    reason: str | None = None
    valid_until: datetime | None = None  # момент, после которого решение точно нужно пересчитать (TTL кеша)

    @classmethod
    def normal(cls) -> AccessDecision:
        return cls(kind=AccessKind.NORMAL)

    @property
    def opens_barrier_automatically(self) -> bool:
        return self.kind is not AccessKind.BLACKLIST


def decide(rules: Iterable[AccessRule], lot_id: UUID, at: datetime) -> AccessDecision:
    """Одно решение из набора правил номера: blacklist > VIP > абонемент > whitelist (max скидка) > NORMAL."""
    rules = list(rules)
    applicable = [r for r in rules if r.applies(lot_id, at)]
    # решение действует до ближайшего изменения набора правил: начала ещё не вступившего правила (машина въехала
    # до старта абонемента, выезжает после) или конца выбранного — иначе кеш отдал бы устаревшее решение
    upcoming = [
        r.valid_from
        for r in rules
        if r.valid_from > at and r.revoked_at is None and (r.lot_id is None or r.lot_id == lot_id)
    ]
    horizon = min(upcoming) if upcoming else None
    if not applicable:
        return AccessDecision(kind=AccessKind.NORMAL, valid_until=horizon)
    chosen = min(applicable, key=lambda r: (PRIORITY[r.kind], -r.discount_percent, r.valid_from))
    ends = [t for t in (chosen.valid_to, horizon) if t is not None]
    return AccessDecision(
        kind=chosen.kind,
        discount_percent=chosen.discount_percent,
        source_id=chosen.source_id,
        rule_id=chosen.id,
        reason=chosen.reason,
        valid_until=min(ends) if ends else None,
    )
