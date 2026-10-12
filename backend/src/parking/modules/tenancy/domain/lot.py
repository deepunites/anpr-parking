"""Площадка и её политики (ASSUMPTIONS A-07…A-13)."""

from __future__ import annotations

import enum
import ipaddress
from dataclasses import dataclass, field, replace
from uuid import UUID

from parking.shared.errors import ValidationError


class UnknownExitPolicy(enum.StrEnum):
    MANUAL_REVIEW = "MANUAL_REVIEW"
    OPEN_FREE = "OPEN_FREE"


@dataclass(frozen=True, slots=True)
class LotPolicies:
    max_session_hours: int = 72
    reentry_window_minutes: int = 5
    unknown_exit_policy: UnknownExitPolicy = UnknownExitPolicy.MANUAL_REVIEW
    deny_entry_when_full: bool = False
    exit_grace_minutes: int = 15
    exit_inferred_after_minutes: int = 60
    late_event_window_hours: int = 24
    barrier_cidr_allowlist: tuple[str, ...] = ()
    show_full_plates: bool = False  # сотрудникам площадки — номер целиком, а не маской (решение клиента, в аудите)

    def validate(self) -> None:
        checks = [
            (1 <= self.max_session_hours <= 24 * 365, "max_session_hours"),
            (0 <= self.reentry_window_minutes <= 60, "reentry_window_minutes"),
            (0 <= self.exit_grace_minutes <= 240, "exit_grace_minutes"),
            (1 <= self.exit_inferred_after_minutes <= 1440, "exit_inferred_after_minutes"),
            (1 <= self.late_event_window_hours <= 168, "late_event_window_hours"),
        ]
        bad = [name for ok, name in checks if not ok]
        if bad:
            raise ValidationError(f"policy out of range: {', '.join(bad)}")
        for cidr in self.barrier_cidr_allowlist:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise ValidationError(f"invalid CIDR in barrier allowlist: {cidr}") from None


@dataclass(slots=True)
class ParkingLot:
    id: UUID
    tenant_id: UUID
    code: str
    name: str
    capacity: int
    timezone: str
    address: str | None = None
    active: bool = True
    policies: LotPolicies = field(default_factory=LotPolicies)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValidationError("capacity must be positive")
        if not 1 <= len(self.code) <= 20:
            raise ValidationError("code must be 1..20 characters")
        self.policies.validate()

    def update(
        self,
        *,
        name: str | None = None,
        capacity: int | None = None,
        address: str | None = None,
        policies: LotPolicies | None = None,
    ) -> None:
        if capacity is not None and capacity <= 0:
            raise ValidationError("capacity must be positive")
        if policies is not None:
            policies.validate()
            self.policies = replace(policies)
        if name is not None:
            self.name = name
        if capacity is not None:
            self.capacity = capacity
        if address is not None:
            self.address = address
