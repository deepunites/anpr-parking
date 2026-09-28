from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest

from parking.modules.access.application.contracts import AccessDecision, AccessKind
from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.parking.application.contracts import EntryCommand, ExitCommand
from parking.runtime.container import Container, build_device_repos
from parking.shared.plate import PlateNumber
from tests.conftest import CAM_A1_IN, LOT_A1, TENANT_A
from tests.support.fakes import FakeBarrierDriver, fake_registry, no_sleep

CAM_A1_OUT = UUID("00000000-0000-4000-8000-00000000c102")


def plate(n: int | str = 1) -> PlateNumber:
    return PlateNumber.parse(f"01T{int(n) if str(n).isdigit() else 1:03d}TT" if str(n).isdigit() else str(n))


def entry_cmd(
    pl: PlateNumber,
    *,
    at: datetime | None = None,
    decision: AccessDecision | None = None,
    camera: UUID | None = CAM_A1_IN,
    **kw: object,
) -> EntryCommand:
    return EntryCommand(
        tenant_id=TENANT_A,
        lot_id=LOT_A1,
        plate=pl,
        event_time=at or datetime.now(tz=UTC),
        decision=decision or AccessDecision.normal(),
        camera_id=camera,
        event_id=uuid4(),
        **kw,
    )  # type: ignore[arg-type]


def exit_cmd(
    pl: PlateNumber,
    *,
    at: datetime | None = None,
    decision: AccessDecision | None = None,
    camera: UUID | None = CAM_A1_OUT,
    **kw: object,
) -> ExitCommand:
    return ExitCommand(
        tenant_id=TENANT_A,
        lot_id=LOT_A1,
        plate=pl,
        event_time=at or datetime.now(tz=UTC),
        event_id=uuid4(),
        decision=decision or AccessDecision.normal(),
        camera_id=camera,
        **kw,
    )  # type: ignore[arg-type]


def subscription_decision() -> AccessDecision:
    return AccessDecision(
        kind=AccessKind.SUBSCRIPTION,
        discount_percent=100,
        source_id=uuid4(),
        rule_id=uuid4(),
        valid_until=datetime.now(tz=UTC) + timedelta(days=10),
    )


@pytest.fixture
async def executor(container: Container) -> tuple[BarrierCommandExecutor, FakeBarrierDriver]:
    registry, driver = fake_registry()
    return BarrierCommandExecutor(
        uow_factory=container.uow, repos=build_device_repos, drivers=registry, clock=container.clock, sleep=no_sleep
    ), driver


@pytest.fixture
async def clean_lot(owner: asyncpg.Connection, container: Container) -> AsyncIterator[None]:
    """Площадка A1 без активных сессий и с чистым счётчиком occupancy."""
    await owner.execute(
        "UPDATE parking_session SET status = 'CANCELLED', version = version + 1 WHERE lot_id = $1 AND status IN ('ACTIVE', 'MANUAL_REVIEW')",
        LOT_A1,
    )
    await owner.execute(
        "UPDATE exit_saga SET state = 'CANCELLED' WHERE lot_id = $1 AND state NOT IN ('COMPLETED', 'EXPIRED', 'CANCELLED')",
        LOT_A1,
    )
    await container.occupancy.set(LOT_A1, 0)
    yield
