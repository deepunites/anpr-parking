"""Демо-данные недели для стенда (v2 этап 7): въезды, выезды, оплаты наличными, смены кассира (одна с недостачей)
и ручные открытия стрелы — через настоящие сервисы со сдвинутыми часами, чтобы дашборд показал «цифры недели».

Только dev/стенд: на PARKING_ENV=prod скрипт отказывается работать. Номера — случайные валидные, не реальные.

    uv run python scripts/demo_week.py            # из окружения стенда (переменные PARKING_*)
"""

from __future__ import annotations

import asyncio
import random
import string
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

TENANT_A = UUID("00000000-0000-4000-8000-0000000000a1")
USER_A_ADMIN = UUID("00000000-0000-4000-8000-0000000a0001")
LOTS = (
    # площадка, камера въезда, камера выезда
    (
        UUID("00000000-0000-4000-8000-00000000a101"),
        UUID("00000000-0000-4000-8000-00000000c101"),
        UUID("00000000-0000-4000-8000-00000000c102"),
    ),
    (
        UUID("00000000-0000-4000-8000-00000000a102"),
        UUID("00000000-0000-4000-8000-00000000c103"),
        UUID("00000000-0000-4000-8000-00000000c104"),
    ),
)
BARRIER_A1_IN = UUID("00000000-0000-4000-8000-00000000e101")
PEAKS = (9, 13, 18)
CASH_SQL = (
    "SELECT coalesce(sum(amount), 0) FROM payment WHERE shift_id = :s AND method = 'CASH' "
    "AND status IN ('PAID', 'REFUNDED')"
)


@dataclass
class ShiftingClock:
    """Часы, которые скрипт двигает по событиям недели."""

    at: datetime

    def now(self) -> datetime:
        return self.at


def _plate(rng: random.Random) -> str:
    letters = string.ascii_uppercase.replace("I", "").replace("O", "")
    region = rng.choice(["01", "10", "20", "30", "40", "60", "70", "80", "90"])
    return f"{region}{rng.choice(letters)}{rng.randint(100, 999)}{rng.choice(letters)}{rng.choice(letters)}"


def _entry_hour(rng: random.Random) -> float:
    peak = rng.choice(PEAKS)
    return min(20.5, max(7.0, rng.gauss(peak, 1.4)))


async def seed(days: int = 7, per_day: tuple[int, int] = (14, 26), seed_value: int = 7) -> dict[str, int]:  # noqa: PLR0912, PLR0915 — сценарий недели читается сверху вниз
    from sqlalchemy import text

    from parking.modules.access.application.contracts import AccessDecision
    from parking.modules.identity.application.contracts import CurrentUser, Role
    from parking.modules.identity.application.user_service import CreateUserCommand
    from parking.modules.parking.application.contracts import EntryCommand, ExitCommand, ExitStatus
    from parking.platform.config import Environment, load_settings
    from parking.platform.db.engine import Pool, build_database
    from parking.runtime.container import build_container
    from parking.shared.plate import PlateNumber

    settings = load_settings()
    if settings.env is Environment.PROD:
        raise SystemExit("demo_week: отказ — PARKING_ENV=prod")
    rng = random.Random(seed_value)  # noqa: S311 — демо-данные, не криптография
    tz = ZoneInfo("Asia/Tashkent")
    clock = ShiftingClock(datetime.now(tz=UTC))
    database = build_database(settings)
    container = build_container(settings, database, clock=clock)
    maint = database.session_factory(Pool.MAINTENANCE)
    admin = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.PLATFORM_ADMIN, lot_scope=frozenset())
    cashier_user = await container.users.create(
        admin,
        CreateUserCommand(
            email=f"demo.cashier.{uuid4().hex[:6]}@alpha-parking.uz",
            password=uuid4().hex,
            role=Role.CASHIER,
            display_name="Дилноза (демо-кассир)",
        ),
        reason="демо-данные недели",
    )
    cashier2_user = await container.users.create(
        admin,
        CreateUserCommand(
            email=f"demo.cashier2.{uuid4().hex[:6]}@alpha-parking.uz",
            password=uuid4().hex,
            role=Role.CASHIER,
            display_name="Бехзод (демо-кассир)",
        ),
        reason="демо-данные недели",
    )
    operator_user = await container.users.create(
        admin,
        CreateUserCommand(
            email=f"demo.operator.{uuid4().hex[:6]}@alpha-parking.uz",
            password=uuid4().hex,
            role=Role.OPERATOR,
            display_name="Азиз (демо-оператор)",
        ),
        reason="демо-данные недели",
    )
    cashiers = {  # у каждой площадки свой кассир: смена открыта на одной площадке
        LOTS[0][0]: CurrentUser(user_id=cashier_user.id, tenant_id=TENANT_A, role=Role.CASHIER, lot_scope=frozenset()),
        LOTS[1][0]: CurrentUser(user_id=cashier2_user.id, tenant_id=TENANT_A, role=Role.CASHIER, lot_scope=frozenset()),
    }
    operator = CurrentUser(user_id=operator_user.id, tenant_id=TENANT_A, role=Role.OPERATOR, lot_scope=frozenset())
    stats = {"entries": 0, "paid": 0, "manual_opens": 0, "shifts": 0}
    today = datetime.now(tz=tz).date()
    try:
        for back in range(days, 0, -1):
            day = today - timedelta(days=back)
            midnight = datetime.combine(day, time.min, tz).astimezone(UTC)

            def at(h: float, base: datetime = midnight) -> datetime:
                return base + timedelta(hours=h)

            events: list[tuple[datetime, str, object]] = []
            for lot_id, cam_in, cam_out in LOTS:
                for _ in range(rng.randint(*per_day)):
                    start = _entry_hour(rng)
                    stay = rng.choice([0.4, 0.8, 1.2, 1.7, 2.5, 3.5, 5.0]) * rng.uniform(0.8, 1.2)
                    plate = PlateNumber.parse(_plate(rng))
                    events.append((at(start), "in", (lot_id, cam_in, plate)))
                    events.append((at(min(start + stay, 21.5)), "out", (lot_id, cam_out, plate)))
            opens = rng.randint(1, 3) if back != 2 else 22  # один «неспокойный» день — сигнал «много ручных открытий»
            for _ in range(opens):
                events.append((at(rng.uniform(8, 21)), "open", None))
            events.sort(key=lambda e: e[0])

            clock.at = at(7.5)
            shifts = {
                lot: await container.cashier.open_shift(c, lot, opening_float=200_000_00, notes="демо")
                for lot, c in cashiers.items()
            }
            for ts, kind, payload in events:
                clock.at = ts
                if kind == "in":
                    lot_id, cam, plate = payload  # type: ignore[misc]
                    await container.parking.process_entry(
                        EntryCommand(
                            tenant_id=TENANT_A,
                            lot_id=lot_id,
                            plate=plate,
                            event_time=ts,
                            decision=AccessDecision.normal(),
                            camera_id=cam,
                            event_id=uuid4(),
                        )
                    )
                    stats["entries"] += 1
                elif kind == "out":
                    lot_id, cam, plate = payload  # type: ignore[misc]
                    result = await container.parking.start_exit(
                        ExitCommand(
                            tenant_id=TENANT_A,
                            lot_id=lot_id,
                            plate=plate,
                            event_time=ts,
                            event_id=uuid4(),
                            decision=AccessDecision.normal(),
                            camera_id=cam,
                            late=True,  # задним числом: сессия закрывается без команды стреле
                        )
                    )
                    if result.status in {ExitStatus.PAYMENT_REQUIRED, ExitStatus.LATE_ACCEPTED} and result.saga_id:
                        await container.parking.advance_saga(TENANT_A, result.saga_id)
                        async with maint() as s:
                            pid = (
                                await s.execute(
                                    text("SELECT id FROM payment WHERE saga_id = :s"), {"s": result.saga_id}
                                )
                            ).scalar()
                        if pid is not None:
                            await container.cashier.confirm_cash(
                                cashiers[lot_id], pid, declared_amount=None, reason=None
                            )
                            stats["paid"] += 1
                else:
                    await container.devices.open_barrier(
                        operator, BARRIER_A1_IN, reason="водитель без номера / пропуск"
                    )
                    stats["manual_opens"] += 1
            clock.at = at(21.8)
            for lot, shift in shifts.items():
                async with maint() as s:
                    cash = (await s.execute(text(CASH_SQL), {"s": shift.id})).scalar() or 0
                shortage = 50_000_00 if back == 3 and lot == LOTS[0][0] else 0  # один день — недостача 50 000 сум
                counted = shift.opening_float + int(cash) - shortage
                await container.cashier.close_shift(cashiers[lot], shift.id, counted_cash=counted, notes="демо")
                stats["shifts"] += 1
    finally:
        await container.aclose()
        await database.dispose()
    return stats


if __name__ == "__main__":
    print(asyncio.run(seed()))
