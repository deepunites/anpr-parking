from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.parking.application.ports import DueSaga, VehicleAttributes, VehicleCard, VehicleRef
from parking.modules.parking.domain.saga import ExitSaga, SagaState
from parking.modules.parking.domain.session import ExitKind, ParkingSession, ReviewReason, SessionStatus
from parking.shared.correlation import get_correlation_id
from parking.shared.errors import ConflictError

_SESSION_COLUMNS = """
    s.id, s.tenant_id, s.lot_id, s.vehicle_id, s.entry_event_id, s.exit_event_id, s.entry_camera_id, s.exit_camera_id, s.entry_time, s.exit_time,
    s.billing_minutes, s.tariff_plan_version_id, s.status::text AS status, s.exit_kind::text AS exit_kind, s.entry_access_kind, s.entry_unknown,
    s.entry_inferred, s.entry_manual, s.review_reason, s.closed_reason, s.version, s.created_at
"""
OCCUPYING = "(s.status = 'ACTIVE' OR (s.status = 'MANUAL_REVIEW' AND s.review_reason LIKE 'EXIT_%'))"


def _session(r: Any) -> ParkingSession:
    return ParkingSession(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        vehicle_id=r.vehicle_id,
        entry_time=r.entry_time,
        status=SessionStatus(r.status),
        entry_camera_id=r.entry_camera_id,
        entry_event_id=r.entry_event_id,
        exit_camera_id=r.exit_camera_id,
        exit_event_id=r.exit_event_id,
        exit_time=r.exit_time,
        billing_minutes=r.billing_minutes,
        tariff_plan_version_id=r.tariff_plan_version_id,
        exit_kind=ExitKind(r.exit_kind) if r.exit_kind else None,
        entry_access_kind=r.entry_access_kind,
        entry_unknown=r.entry_unknown,
        entry_inferred=r.entry_inferred,
        entry_manual=r.entry_manual,
        review_reason=ReviewReason(r.review_reason) if r.review_reason else None,
        closed_reason=r.closed_reason,
        version=r.version,
        created_at=r.created_at,
    )


class SqlVehicleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self, plate_hash: bytes, plate_encrypted: bytes, enc_key_version: int, seen_at: datetime
    ) -> VehicleRef:
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO vehicle (tenant_id, plate_hash, plate_encrypted, enc_key_version, first_seen_at, last_seen_at)
                    VALUES (app.current_tenant(), :hash, :enc, :version, :seen, :seen)
                    ON CONFLICT (tenant_id, plate_hash) DO UPDATE SET last_seen_at = GREATEST(vehicle.last_seen_at, EXCLUDED.last_seen_at)
                    RETURNING id, plate_hash
                    """
                ),
                {"hash": plate_hash, "enc": plate_encrypted, "version": enc_key_version, "seen": seen_at},
            )
        ).one()
        return VehicleRef(id=row.id, plate_hash=bytes(row.plate_hash))

    async def plate_encrypted(self, vehicle_id: UUID) -> tuple[bytes, int] | None:
        row = (
            await self._s.execute(
                text("SELECT plate_encrypted, enc_key_version FROM vehicle WHERE id = :id"), {"id": vehicle_id}
            )
        ).first()
        return None if row is None else (bytes(row.plate_encrypted), int(row.enc_key_version))

    async def card(self, vehicle_id: UUID) -> VehicleCard | None:
        row = (
            await self._s.execute(
                text("SELECT plate_encrypted, enc_key_version, make, model, color FROM vehicle WHERE id = :id"),
                {"id": vehicle_id},
            )
        ).first()
        if row is None:
            return None
        return VehicleCard(
            plate_encrypted=bytes(row.plate_encrypted),
            enc_key_version=int(row.enc_key_version),
            attributes=VehicleAttributes(make=row.make, model=row.model, color=row.color),
        )

    async def set_attributes(
        self, vehicle_id: UUID, *, make: str | None, model: str | None, color: str | None, at: datetime
    ) -> None:
        await self._s.execute(
            text(
                "UPDATE vehicle SET make = :make, model = :model, color = :color, attributes_updated_at = :at "
                "WHERE id = :id AND anonymized_at IS NULL"
            ),
            {"id": vehicle_id, "make": make, "model": model, "color": color, "at": at},
        )

    async def attributes_by_hashes(self, plate_hashes: list[bytes]) -> dict[bytes, VehicleAttributes]:
        if not plate_hashes:
            return {}
        rows = (
            await self._s.execute(
                text("SELECT plate_hash, make, model, color FROM vehicle WHERE plate_hash = ANY(:hashes)"),
                {"hashes": plate_hashes},
            )
        ).all()
        return {bytes(r.plate_hash): VehicleAttributes(make=r.make, model=r.model, color=r.color) for r in rows}


class SqlSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, s: ParkingSession) -> None:
        params = {
            "id": s.id,
            "tenant": s.tenant_id,
            "lot": s.lot_id,
            "vehicle": s.vehicle_id,
            "entry_event": s.entry_event_id,
            "entry_camera": s.entry_camera_id,
            "entry_time": s.entry_time,
            "status": s.status.value,
            "access_kind": s.entry_access_kind,
            "entry_unknown": s.entry_unknown,
            "entry_inferred": s.entry_inferred,
            "entry_manual": s.entry_manual,
            "review": s.review_reason.value if s.review_reason else None,
            "version": s.version,
        }
        # I-1 через арбитр частичного индекса uq_session_one_active: конфликт не ломает транзакцию вызывающего и не
        # требует savepoint (2 лишних round-trip на каждый ENTRY) — при конфликте строка не вставляется и RETURNING пуст.
        inserted = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO parking_session (id, tenant_id, lot_id, vehicle_id, entry_event_id, entry_camera_id, entry_time, status,
                                                 entry_access_kind, entry_unknown, entry_inferred, entry_manual, review_reason, version)
                    VALUES (:id, :tenant, :lot, :vehicle, :entry_event, :entry_camera, :entry_time, CAST(:status AS session_status),
                            :access_kind, :entry_unknown, :entry_inferred, :entry_manual, :review, :version)
                    ON CONFLICT (lot_id, vehicle_id) WHERE status = 'ACTIVE' DO NOTHING
                    RETURNING id
                    """
                ),
                params,
            )
        ).first()
        if inserted is None:
            raise ConflictError("active session already exists for this vehicle on the lot (I-1)")
        s.created_at = s.created_at or s.entry_time

    async def get(self, session_id: UUID) -> ParkingSession | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE s.id = :id"), {"id": session_id}
            )
        ).first()
        return None if row is None else _session(row)

    async def get_for_update(self, session_id: UUID) -> ParkingSession | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE s.id = :id FOR UPDATE"),
                {"id": session_id},
            )
        ).first()
        return None if row is None else _session(row)

    async def save(self, s: ParkingSession) -> None:
        result = await self._s.execute(
            text(
                """
                UPDATE parking_session SET vehicle_id = :vehicle, exit_event_id = :exit_event, exit_camera_id = :exit_camera, exit_time = :exit_time,
                    billing_minutes = :billing, tariff_plan_version_id = :tpv, status = CAST(:status AS session_status),
                    exit_kind = CAST(:exit_kind AS exit_kind), review_reason = :review, closed_reason = :closed_reason, version = :version
                WHERE id = :id AND version = :expected
                """
            ),
            {
                "id": s.id,
                "vehicle": s.vehicle_id,
                "exit_event": s.exit_event_id,
                "exit_camera": s.exit_camera_id,
                "exit_time": s.exit_time,
                "billing": s.billing_minutes,
                "tpv": s.tariff_plan_version_id,
                "status": s.status.value,
                "exit_kind": s.exit_kind.value if s.exit_kind else None,
                "review": s.review_reason.value if s.review_reason else None,
                "closed_reason": s.closed_reason,
                "version": s.version,
                "expected": s.version - 1,
            },
        )
        if getattr(result, "rowcount", 0) != 1:
            raise ConflictError(f"session {s.id} was modified concurrently")

    async def active_for_vehicle(
        self, lot_id: UUID, vehicle_id: UUID, *, for_update: bool = False
    ) -> ParkingSession | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE s.lot_id = :lot AND s.vehicle_id = :vehicle AND s.status = 'ACTIVE'{lock}"
                ),
                {"lot": lot_id, "vehicle": vehicle_id},
            )
        ).first()
        return None if row is None else _session(row)

    async def open_for_plate(
        self, lot_id: UUID, plate_hashes: list[bytes], *, for_update: bool = False
    ) -> ParkingSession | None:
        lock = " FOR UPDATE OF s" if for_update else ""
        row = (
            await self._s.execute(
                text(
                    f"""
                    SELECT {_SESSION_COLUMNS} FROM vehicle v
                    JOIN parking_session s ON s.vehicle_id = v.id AND s.lot_id = :lot AND {OCCUPYING}
                    WHERE v.plate_hash = ANY(:hashes)
                    ORDER BY s.entry_time DESC LIMIT 1{lock}
                    """
                ),
                {"lot": lot_id, "hashes": plate_hashes},
            )
        ).first()
        return None if row is None else _session(row)

    async def list_by_lot(
        self,
        lot_id: UUID,
        *,
        statuses: tuple[SessionStatus, ...] | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[ParkingSession]:
        conditions = ["s.lot_id = :lot"]
        params: dict[str, Any] = {"lot": lot_id, "limit": limit}
        if statuses:
            conditions.append("s.status = ANY(CAST(:statuses AS session_status[]))")
            params["statuses"] = [st.value for st in statuses]
        if after is not None:
            conditions.append("(s.entry_time, s.id) < (:after_at, :after_id)")
            params.update({"after_at": after[0], "after_id": after[1]})
        query = f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE {' AND '.join(conditions)} ORDER BY s.entry_time DESC, s.id DESC LIMIT :limit"
        return [_session(r) for r in await self._s.execute(text(query), params)]

    async def list_by_plate_hashes(self, plate_hashes: list[bytes], *, limit: int) -> list[ParkingSession]:
        rows = await self._s.execute(
            text(
                f"""
                SELECT {_SESSION_COLUMNS} FROM vehicle v JOIN parking_session s ON s.vehicle_id = v.id
                WHERE v.plate_hash = ANY(:hashes) ORDER BY s.entry_time DESC LIMIT :limit
                """
            ),
            {"hashes": plate_hashes, "limit": limit},
        )
        return [_session(r) for r in rows]

    async def review_queue(self, lot_ids: frozenset[UUID] | None, *, limit: int) -> list[ParkingSession]:
        params: dict[str, Any] = {"limit": limit}
        scope = ""
        if lot_ids is not None:
            scope = " AND s.lot_id = ANY(:lots)"
            params["lots"] = list(lot_ids)
        rows = await self._s.execute(
            text(
                f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE s.status = 'MANUAL_REVIEW'{scope} ORDER BY s.updated_at LIMIT :limit"
            ),
            params,
        )
        return [_session(r) for r in rows]

    async def count_occupying(self, lot_id: UUID) -> int:
        row = (
            await self._s.execute(
                text(f"SELECT count(*) AS n FROM parking_session s WHERE s.lot_id = :lot AND {OCCUPYING}"),
                {"lot": lot_id},
            )
        ).one()
        return int(row.n)

    async def count_occupying_by_entry_camera(self, lot_id: UUID) -> dict[UUID | None, int]:
        """v2 этап 5: занятость в разрезе камеры въезда — карта раскладывает её по зонам схемы."""
        rows = await self._s.execute(
            text(
                f"SELECT s.entry_camera_id AS camera, count(*) AS n FROM parking_session s "
                f"WHERE s.lot_id = :lot AND {OCCUPYING} GROUP BY s.entry_camera_id"
            ),
            {"lot": lot_id},
        )
        return {r.camera: int(r.n) for r in rows}

    async def exiting_session(
        self, lot_id: UUID, vehicle_id: UUID, *, exited_since: datetime, saga_since: datetime
    ) -> UUID | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT s.id FROM parking_session s
                    WHERE s.lot_id = :lot AND s.vehicle_id = :v AND (
                        s.exit_time >= :exited
                        OR (s.entry_unknown AND s.status = 'MANUAL_REVIEW' AND s.entry_time >= :exited)
                        OR EXISTS (
                            SELECT 1 FROM exit_saga g WHERE g.session_id = s.id
                              AND g.state NOT IN ('COMPLETED', 'EXPIRED', 'CANCELLED') AND g.exit_event_time >= :saga
                        )
                    )
                    ORDER BY s.entry_time DESC LIMIT 1
                    """
                ),
                {"lot": lot_id, "v": vehicle_id, "exited": exited_since, "saga": saga_since},
            )
        ).first()
        return None if row is None else row.id

    async def open_in_lot_before(self, lot_id: UUID, older_than: datetime, *, limit: int) -> list[ParkingSession]:
        rows = await self._s.execute(
            text(
                f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE s.lot_id = :lot "
                "AND s.status IN ('ACTIVE', 'MANUAL_REVIEW') AND s.entry_time < :older ORDER BY s.entry_time LIMIT :limit"
            ),
            {"lot": lot_id, "older": older_than, "limit": limit},
        )
        return [_session(r) for r in rows]

    async def stale_active(self, older_than: datetime, *, limit: int) -> list[ParkingSession]:
        rows = await self._s.execute(
            text(
                f"SELECT {_SESSION_COLUMNS} FROM parking_session s WHERE s.status = 'ACTIVE' AND s.entry_time < :older ORDER BY s.entry_time LIMIT :limit"
            ),
            {"older": older_than, "limit": limit},
        )
        return [_session(r) for r in rows]


_SAGA_COLUMNS = """
    id, tenant_id, lot_id, session_id, state::text AS state, step_deadline, saga_deadline, exit_event_id, exit_event_time, additional_event_ids,
    access_decision, calculation, amount_due, payment_id, barrier_command_id, barrier_result, attempts, last_error, review_reason, correlation_id,
    created_at, updated_at, exit_camera_id, flags
"""


def _json(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value)


def _saga(r: Any) -> ExitSaga:
    return ExitSaga(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        session_id=r.session_id,
        exit_event_id=r.exit_event_id,
        exit_event_time=r.exit_event_time,
        access_decision=_json(r.access_decision),
        calculation=_json(r.calculation),
        amount_due=int(r.amount_due),
        correlation_id=r.correlation_id,
        created_at=r.created_at,
        state=SagaState(r.state),
        step_deadline=r.step_deadline,
        saga_deadline=r.saga_deadline,
        exit_camera_id=r.exit_camera_id,
        additional_event_ids=list(r.additional_event_ids or []),
        flags=_json(r.flags),
        payment_id=r.payment_id,
        barrier_command_id=r.barrier_command_id,
        barrier_result=r.barrier_result,
        attempts=r.attempts,
        last_error=r.last_error,
        review_reason=r.review_reason,
        updated_at=r.updated_at,
    )


class SqlSagaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, g: ExitSaga) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO exit_saga (id, tenant_id, lot_id, session_id, state, step_deadline, saga_deadline, exit_event_id, exit_event_time,
                                       additional_event_ids, access_decision, calculation, amount_due, correlation_id, created_at, exit_camera_id, flags)
                VALUES (:id, :tenant, :lot, :session, CAST(:state AS saga_state), :step_deadline, :saga_deadline, :exit_event, :exit_time,
                        :additional, CAST(:decision AS jsonb), CAST(:calc AS jsonb), :amount, :correlation, :created_at, :exit_camera, CAST(:flags AS jsonb))
                """
            ),
            {
                "id": g.id,
                "tenant": g.tenant_id,
                "lot": g.lot_id,
                "session": g.session_id,
                "state": g.state.value,
                "step_deadline": g.step_deadline,
                "saga_deadline": g.saga_deadline,
                "exit_event": g.exit_event_id,
                "exit_time": g.exit_event_time,
                "additional": g.additional_event_ids,
                "decision": json.dumps(g.access_decision),
                "calc": json.dumps(g.calculation, default=str),
                "amount": g.amount_due,
                "correlation": g.correlation_id,
                "created_at": g.created_at,
                "exit_camera": g.exit_camera_id,
                "flags": json.dumps(g.flags),
            },
        )

    async def get(self, saga_id: UUID) -> ExitSaga | None:
        row = (
            await self._s.execute(text(f"SELECT {_SAGA_COLUMNS} FROM exit_saga WHERE id = :id"), {"id": saga_id})
        ).first()
        return None if row is None else _saga(row)

    async def get_for_update(self, saga_id: UUID) -> ExitSaga | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_SAGA_COLUMNS} FROM exit_saga WHERE id = :id FOR UPDATE"), {"id": saga_id}
            )
        ).first()
        return None if row is None else _saga(row)

    async def active_for_session(self, session_id: UUID, *, for_update: bool = False) -> ExitSaga | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_SAGA_COLUMNS} FROM exit_saga WHERE session_id = :s AND state NOT IN ('COMPLETED', 'EXPIRED', 'CANCELLED'){lock}"
                ),
                {"s": session_id},
            )
        ).first()
        return None if row is None else _saga(row)

    async def save(self, g: ExitSaga) -> None:
        await self._s.execute(
            text(
                """
                UPDATE exit_saga SET state = CAST(:state AS saga_state), step_deadline = :step_deadline, additional_event_ids = :additional,
                    payment_id = :payment, barrier_command_id = :command, barrier_result = :result, attempts = :attempts, last_error = :error,
                    review_reason = :review, flags = CAST(:flags AS jsonb)
                WHERE id = :id
                """
            ),
            {
                "id": g.id,
                "state": g.state.value,
                "step_deadline": g.step_deadline,
                "additional": g.additional_event_ids,
                "payment": g.payment_id,
                "command": g.barrier_command_id,
                "result": g.barrier_result,
                "attempts": g.attempts,
                "error": g.last_error,
                "review": g.review_reason,
                "flags": json.dumps(g.flags),
            },
        )

    async def review_queue(self, lot_ids: frozenset[UUID] | None, *, limit: int) -> list[ExitSaga]:
        params: dict[str, Any] = {"limit": limit}
        scope = ""
        if lot_ids is not None:
            scope = " AND lot_id = ANY(:lots)"
            params["lots"] = list(lot_ids)
        rows = await self._s.execute(
            text(
                f"SELECT {_SAGA_COLUMNS} FROM exit_saga WHERE state = 'MANUAL_REVIEW'{scope} ORDER BY updated_at LIMIT :limit"
            ),
            params,
        )
        return [_saga(r) for r in rows]


class SqlSagaScanner:
    """Кросс-tenant поиск просроченных саг — роль app_maintenance, собственные короткие транзакции."""

    def __init__(self, maintenance_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = maintenance_factory

    async def due(self, now: datetime, *, limit: int) -> list[DueSaga]:
        async with self._factory() as session, session.begin():
            rows = await session.execute(
                text(
                    """
                    SELECT tenant_id, id FROM exit_saga
                    WHERE state NOT IN ('COMPLETED', 'EXPIRED', 'CANCELLED', 'MANUAL_REVIEW') AND step_deadline + interval '10 seconds' <= :now
                    ORDER BY step_deadline LIMIT :limit
                    """
                ),
                {"now": now, "limit": limit},
            )
            return [DueSaga(tenant_id=r.tenant_id, saga_id=r.id) for r in rows]

    async def active_lots(self) -> list[tuple[UUID, UUID]]:
        async with self._factory() as session, session.begin():
            rows = await session.execute(
                text("SELECT tenant_id, id FROM parking_lot WHERE active ORDER BY tenant_id, id")
            )
            return [(r.tenant_id, r.id) for r in rows]


class SqlCorrectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        correction_id: UUID,
        session: ParkingSession,
        event_id: UUID | None,
        old_vehicle_id: UUID,
        new_vehicle_id: UUID,
        plate_encrypted: bytes,
        enc_key_version: int,
        reason: str,
        user_id: UUID,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO manual_correction (id, tenant_id, lot_id, session_id, event_id, old_vehicle_id, new_vehicle_id, corrected_plate_encrypted,
                                               enc_key_version, reason, user_id, correlation_id)
                VALUES (:id, :tenant, :lot, :session, :event, :old, :new, :enc, :version, :reason, :user, :correlation)
                """
            ),
            {
                "id": correction_id,
                "tenant": session.tenant_id,
                "lot": session.lot_id,
                "session": session.id,
                "event": event_id,
                "old": old_vehicle_id,
                "new": new_vehicle_id,
                "enc": plate_encrypted,
                "version": enc_key_version,
                "reason": reason,
                "user": user_id,
                "correlation": UUID(get_correlation_id()),
            },
        )
