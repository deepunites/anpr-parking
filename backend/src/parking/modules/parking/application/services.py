"""ENTRY, EXIT-сага и ручные операции оператора.

Каждый метод — ровно одна транзакция; внешних вызовов внутри нет (I-9).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from parking.modules.access.application.contracts import AccessDecision, AccessKind, AccessResolver
from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.billing.application.contracts import PaymentObligations, PaymentStatus, PrivilegeMethod
from parking.modules.devices.application.contracts import BarrierCommands, CommandSource, CommandState
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.contracts import (
    EntryCommand,
    EntryResult,
    EntryStatus,
    ExitCommand,
    ExitQuote,
    ExitResult,
    ExitStatus,
    ManualEntryCommand,
    SagaView,
    SessionView,
    SimilarSession,
    StaleSessionsResult,
    VehicleAttributesView,
    VehicleView,
)
from parking.modules.parking.application.ports import (
    CorrectionRepository,
    OccupancyProjection,
    SagaRepository,
    SagaScanner,
    SessionRepository,
    VehicleAttributes,
    VehicleRepository,
)
from parking.modules.parking.domain.saga import ExitSaga, SagaState
from parking.modules.parking.domain.session import (
    ExitKind,
    ParkingSession,
    PlateCorrected,
    ReviewReason,
    SessionClosed,
    SessionMovedToReview,
    SessionOrphaned,
    SessionStatus,
)
from parking.modules.parking.domain.similar import SIMILAR_MAX, plate_distance
from parking.modules.tariff.application.contracts import AccessKind as TariffAccessKind
from parking.modules.tariff.application.contracts import AccessTerms, TariffCalculator
from parking.modules.tenancy.application.contracts import LotDirectory, LotInfo, UnknownExitPolicy
from parking.platform import metrics
from parking.platform.crypto import PlateCrypto
from parking.platform.db.uow import UnitOfWork
from parking.platform.events import IntegrationEvent
from parking.platform.logging import get_logger
from parking.platform.tracing import FLOW_ATTR, span
from parking.shared.clock import Clock
from parking.shared.correlation import get_correlation_id
from parking.shared.errors import ConflictError, NotFoundError, ValidationError
from parking.shared.plate import PlateNumber

log = get_logger("parking")


@dataclass(frozen=True, slots=True)
class ParkingRepositories:
    vehicles: VehicleRepository
    sessions: SessionRepository
    sagas: SagaRepository
    corrections: CorrectionRepository
    lots: LotDirectory
    barriers: BarrierCommands
    payments: PaymentObligations
    tariff: TariffCalculator
    audit: AuditWriter


UowFactory = Callable[[UUID | None], UnitOfWork]
ReposFactory = Callable[[UnitOfWork], ParkingRepositories]


# Роли, которые работают с машинами на площадке: политика площадки «полные номера» открывает номер только им —
# разработчику интеграций, наблюдателю и ролям платформы по-прежнему маска (PRIVACY §1, минимизация).
FULL_PLATE_ROLES = frozenset({Role.OWNER, Role.TENANT_ADMIN, Role.MANAGER, Role.OPERATOR, Role.CASHIER})
# Эхо выезда (стрела одна на въезд и выезд): камера въезда видит выезжающую машину через секунды после камеры выезда.
EXIT_ECHO = timedelta(seconds=60)  # после выезда (или «выезда без въезда»)
EXIT_WAIT = timedelta(minutes=30)  # пока идёт выезд — оплата у кассира, открытие стрелы
STALE_BATCH = 2000  # за один раз; больше — повторить (останутся самые новые из старых)
# closed_reason сессии — код (varchar(40), как REENTRY / MAX_SESSION_HOURS); причину сотрудника хранит аудит (I-15)
CLOSED_MANUAL, ORPHANED_MANUAL, STALE_MANUAL = "CLOSED_MANUAL", "ORPHANED_MANUAL", "STALE_MANUAL"
EXIT_MATCHED = "EXIT_MATCHED"  # «выезд без въезда» оказался машиной на площадке — номер прочитан с ошибкой
SIMILAR_LIMIT, SIMILAR_SCAN = 5, 3000  # кандидатов кассиру; машин на площадке, среди которых ищем


def _key(*parts: object) -> bytes:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()


def _terms(decision: AccessDecision) -> AccessTerms:
    return AccessTerms(kind=TariffAccessKind(decision.kind.value), discount_percent=decision.discount_percent)


class ParkingService:
    """Реализация ParkingCommands + ручные операции."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repos: ReposFactory,
        crypto: PlateCrypto,
        occupancy: OccupancyProjection,
        clock: Clock,
        access: AccessResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._crypto = crypto
        self._occupancy = occupancy
        self._clock = clock
        self._access = access

    # ------------------------------------------------------------------ ENTRY
    async def process_entry(self, cmd: EntryCommand) -> EntryResult:
        async with self._uow_factory(cmd.tenant_id) as uow:
            return await self.process_entry_in(uow, cmd)

    async def process_entry_in(self, uow: UnitOfWork, cmd: EntryCommand) -> EntryResult:
        """ENTRY внутри транзакции вызывающего: ANPR ingest пишет идемпотентность, событие и сессию атомарно."""
        with span(
            "entry.process", tenant_id=cmd.tenant_id, lot_id=cmd.lot_id, camera_id=cmd.camera_id, event_type="ENTRY"
        ):
            return await self._process_entry_traced(uow, cmd)

    async def _process_entry_traced(self, uow: UnitOfWork, cmd: EntryCommand) -> EntryResult:
        occupancy = await self._occupancy.get(cmd.lot_id)
        repos = self._repos(uow)
        lot = await repos.lots.get(cmd.lot_id)
        if lot is None or not lot.active:
            raise NotFoundError("lot not found")
        if lot.policies.deny_entry_when_full and occupancy is not None and occupancy >= lot.capacity and not cmd.late:
            log.info("entry_denied_full", lot_id=str(lot.id), occupancy=occupancy)
            return EntryResult(status=EntryStatus.ENTRY_DENIED_FULL, session_id=None)
        plate_hash = self._crypto.hash(cmd.tenant_id, cmd.plate)
        encrypted, version = self._crypto.encrypt(cmd.tenant_id, cmd.plate)
        vehicle = await repos.vehicles.upsert(plate_hash, encrypted, version, cmd.event_time)
        exiting = await repos.sessions.exiting_session(
            cmd.lot_id, vehicle.id, exited_since=cmd.event_time - EXIT_ECHO, saga_since=cmd.event_time - EXIT_WAIT
        )
        if exiting is not None:
            # иначе «повторный въезд» закрыл бы выезжающую сессию вместе с оплатой и открыл стрелу без неё
            log.info("entry_exit_echo", session_id=str(exiting), camera_id=str(cmd.camera_id))
            return EntryResult(status=EntryStatus.ENTRY_EXIT_ECHO, session_id=exiting)

        blacklisted = cmd.decision.kind is AccessKind.BLACKLIST
        status = SessionStatus.ACTIVE
        review: ReviewReason | None = None
        if blacklisted:
            status, review = SessionStatus.MANUAL_REVIEW, ReviewReason.ENTRY_BLACKLIST
        elif cmd.requires_review:
            status, review = SessionStatus.MANUAL_REVIEW, ReviewReason.ENTRY_LOW_CONFIDENCE
        session = ParkingSession(
            id=uuid4(),
            tenant_id=cmd.tenant_id,
            lot_id=cmd.lot_id,
            vehicle_id=vehicle.id,
            entry_time=cmd.event_time,
            status=status,
            entry_camera_id=cmd.camera_id,
            entry_event_id=cmd.event_id,
            entry_access_kind=cmd.decision.kind.value,
            review_reason=review,
        )
        orphaned, duplicate = await self._add_entry_session(uow, repos, lot, cmd, session)
        if duplicate is not None:
            return duplicate

        barrier_command_id: UUID | None = None
        result_status = EntryStatus.ENTRY_CONFIRMED
        if blacklisted:
            result_status = EntryStatus.ENTRY_DENIED  # I-8: автоматического открытия нет на уровне кода
            session.events.append(SessionMovedToReview(session_id=session.id, reason=ReviewReason.ENTRY_BLACKLIST))
        elif cmd.requires_review:
            result_status = EntryStatus.MANUAL_REVIEW
            session.events.append(SessionMovedToReview(session_id=session.id, reason=ReviewReason.ENTRY_LOW_CONFIDENCE))
        elif cmd.late:
            result_status = EntryStatus.LATE_ACCEPTED
        else:
            command = await repos.barriers.request_open(
                lot_id=cmd.lot_id,
                camera_id=cmd.camera_id,
                session_id=session.id,
                saga_id=None,
                source=CommandSource.AUTO_ENTRY,
                idempotency_key=_key("entry", session.id),
                fresh_key=True,  # session.id создан выше в этой же транзакции
            )
            barrier_command_id = command.command_id
        uow.publish(self._session_created(session, cmd.camera_id))
        self._emit_session_events(uow, session)
        if session.occupies_space:
            uow.after_commit(lambda: self._occupancy.adjust(cmd.lot_id, +1))
        return EntryResult(
            status=result_status,
            session_id=session.id,
            barrier_command_id=barrier_command_id,
            orphaned_session_id=orphaned,
        )

    async def _add_entry_session(
        self, uow: UnitOfWork, repos: ParkingRepositories, lot: LotInfo, cmd: EntryCommand, session: ParkingSession
    ) -> tuple[UUID | None, EntryResult | None]:
        """Новая сессия въезда. Открытая сессия той же машины: в окне повторного въезда — дубль (готовый результат
        вторым элементом), позже — машина уезжала мимо камеры: старая → ORPHAN «REENTRY» (её id первым элементом)."""
        try:
            await repos.sessions.add(session)
            return None, None
        except ConflictError:
            existing = await repos.sessions.active_for_vehicle(cmd.lot_id, session.vehicle_id, for_update=True)
            if existing is None:
                raise
            if cmd.event_time - existing.entry_time <= timedelta(minutes=lot.policies.reentry_window_minutes):
                log.info("entry_duplicate_window", session_id=str(existing.id))
                return None, EntryResult(status=EntryStatus.ENTRY_CONFIRMED, session_id=existing.id, duplicate=True)
            existing.mark_orphan("REENTRY")  # A-07: машина уезжала мимо камеры
            await repos.sessions.save(existing)
            self._emit_session_events(uow, existing)
            uow.after_commit(self._decrement(cmd.lot_id))
        await repos.sessions.add(session)
        return existing.id, None

    # ------------------------------------------------------------------- EXIT
    async def start_exit(self, cmd: ExitCommand) -> ExitResult:
        async with self._uow_factory(cmd.tenant_id) as uow:
            return await self.start_exit_in(uow, cmd)

    async def start_exit_in(self, uow: UnitOfWork, cmd: ExitCommand) -> ExitResult:
        """Старт EXIT-саги внутри транзакции вызывающего."""
        with span(
            "exit.start",
            tenant_id=cmd.tenant_id,
            lot_id=cmd.lot_id,
            camera_id=cmd.camera_id,
            event_type="EXIT",
            **{FLOW_ATTR: "exit"},
        ):
            return await self._start_exit_traced(uow, cmd)

    async def _start_exit_traced(self, uow: UnitOfWork, cmd: ExitCommand) -> ExitResult:
        repos = self._repos(uow)
        lot = await repos.lots.get(cmd.lot_id)
        if lot is None or not lot.active:
            raise NotFoundError("lot not found")
        hashes = self._crypto.hashes_for_lookup(cmd.tenant_id, cmd.plate)
        session = await repos.sessions.open_for_plate(cmd.lot_id, hashes, for_update=True)
        if session is None:
            return await self._exit_without_session(uow, repos, lot, cmd)
        return await self._exit_session(uow, repos, session, cmd)

    async def _exit_session(
        self,
        uow: UnitOfWork,
        repos: ParkingRepositories,
        session: ParkingSession,
        cmd: ExitCommand,
        *,
        manual: bool = False,
    ) -> ExitResult:
        """Выезд найденной сессии: сага с тарифом. manual — кассир («Открыть выезд», «Это она»), не камера."""
        active_saga = await repos.sagas.active_for_session(session.id, for_update=True)
        if active_saga is not None:  # A-09: второй EXIT присоединяется к живой саге
            active_saga.attach_event(cmd.event_id)
            await repos.sagas.save(active_saga)
            return ExitResult(
                status=ExitStatus.EXIT_IN_PROGRESS,
                session_id=session.id,
                saga_id=active_saga.id,
                amount_minor=active_saga.amount_due,
                saga_state=active_saga.state,
            )
        if not manual and session.review_reason is ReviewReason.EXIT_UNKNOWN_SESSION:
            # камера снова прочитала тот же ошибочный номер: «выезд без въезда» ждёт кассира, а не уезжает бесплатно
            return ExitResult(status=ExitStatus.EXIT_WITHOUT_SESSION, session_id=session.id)
        if cmd.requires_review:
            session.move_to_review(ReviewReason.EXIT_LOW_CONFIDENCE)
            await repos.sessions.save(session)
            self._emit_session_events(uow, session)
            return ExitResult(status=ExitStatus.MANUAL_REVIEW, session_id=session.id)
        if cmd.decision.kind is AccessKind.BLACKLIST:
            session.move_to_review(ReviewReason.EXIT_BLACKLIST)
            await repos.sessions.save(session)
            self._emit_session_events(uow, session)
            return ExitResult(status=ExitStatus.MANUAL_REVIEW, session_id=session.id)

        flags: dict[str, Any] = {"late": cmd.late}
        prepaid = await repos.payments.prepaid_for(session.id, cmd.event_time)
        if prepaid is not None:
            amount, calculation = (
                0,
                {
                    "amount": 0,
                    "applied_rules": ["PREPAID"],
                    "billing_minutes": ParkingSession.minutes_between(session.entry_time, cmd.event_time),
                    "prepaid_payment_id": str(prepaid.id),
                },
            )
            flags["prepaid"] = True
        elif cmd.event_time <= session.entry_time:  # «выезд без въезда»: машину видели только на выезде — 0 минут
            amount, calculation = 0, {"amount": 0, "applied_rules": ["NO_STAY"], "billing_minutes": 0}
        else:
            calc = await repos.tariff.calculate(cmd.lot_id, session.entry_time, cmd.event_time, _terms(cmd.decision))
            amount, calculation = calc.amount.minor, calc.to_snapshot()
        now = self._clock.now()
        saga = ExitSaga(
            id=uuid4(),
            tenant_id=cmd.tenant_id,
            lot_id=cmd.lot_id,
            session_id=session.id,
            exit_event_id=cmd.event_id,
            exit_event_time=cmd.event_time,
            access_decision=_decision_json(cmd.decision),
            calculation=calculation,
            amount_due=amount,
            correlation_id=UUID(get_correlation_id()),
            created_at=now,
            exit_camera_id=cmd.camera_id,
            flags=flags,
        )
        await repos.sagas.add(saga)
        uow.publish(
            IntegrationEvent(
                type="parking.exit.requested.v1",
                tenant_id=cmd.tenant_id,
                aggregate_type="exit_saga",
                aggregate_id=saga.id,
                payload={
                    "saga_id": str(saga.id),
                    "session_id": str(session.id),
                    "lot_id": str(cmd.lot_id),
                    "amount_due": amount,
                    "access_kind": cmd.decision.kind.value,
                },
            )
        )
        status = (
            ExitStatus.LATE_ACCEPTED
            if cmd.late
            else (ExitStatus.PAYMENT_REQUIRED if amount > 0 else ExitStatus.EXIT_CONFIRMED)
        )
        return ExitResult(
            status=status, session_id=session.id, saga_id=saga.id, amount_minor=amount, saga_state=saga.state
        )

    async def _exit_without_session(
        self, uow: UnitOfWork, repos: ParkingRepositories, lot: LotInfo, cmd: ExitCommand
    ) -> ExitResult:
        """A-08: политика площадки — MANUAL_REVIEW (по умолчанию) или OPEN_FREE (окно запуска)."""
        plate_hash = self._crypto.hash(cmd.tenant_id, cmd.plate)
        encrypted, version = self._crypto.encrypt(cmd.tenant_id, cmd.plate)
        vehicle = await repos.vehicles.upsert(plate_hash, encrypted, version, cmd.event_time)
        session = ParkingSession(
            id=uuid4(),
            tenant_id=cmd.tenant_id,
            lot_id=cmd.lot_id,
            vehicle_id=vehicle.id,
            entry_time=cmd.event_time,
            entry_unknown=True,
            entry_access_kind=cmd.decision.kind.value,
            # камера, увидевшая машину на выезде: её стрела и её видео для разбора кассиром
            entry_camera_id=cmd.camera_id,
            entry_event_id=cmd.event_id,
        )
        if (
            lot.policies.unknown_exit_policy is UnknownExitPolicy.OPEN_FREE
            and cmd.decision.kind is not AccessKind.BLACKLIST
        ):
            session.status = SessionStatus.ACTIVE
            await repos.sessions.add(session)
            session.close(
                exit_time=cmd.event_time,
                billing_minutes=0,
                exit_kind=ExitKind.CAMERA,
                tariff_plan_version_id=None,
                exit_camera_id=cmd.camera_id,
                exit_event_id=cmd.event_id,
                reason="UNKNOWN_EXIT_OPEN_FREE",
            )
            await repos.sessions.save(session)
            command_id = None
            if not cmd.late:
                command = await repos.barriers.request_open(
                    lot_id=cmd.lot_id,
                    camera_id=cmd.camera_id,
                    session_id=session.id,
                    saga_id=None,
                    source=CommandSource.AUTO_EXIT,
                    idempotency_key=_key("exit-free", session.id),
                )
                command_id = command.command_id
            uow.publish(self._session_created(session, cmd.camera_id))
            self._emit_session_events(uow, session)
            log.info("exit_without_session_open_free", session_id=str(session.id), command_id=str(command_id))
            return ExitResult(status=ExitStatus.EXIT_CONFIRMED, session_id=session.id)
        session.status = SessionStatus.MANUAL_REVIEW
        session.review_reason = ReviewReason.EXIT_UNKNOWN_SESSION
        await repos.sessions.add(session)
        session.events.append(SessionMovedToReview(session_id=session.id, reason=ReviewReason.EXIT_UNKNOWN_SESSION))
        uow.publish(self._session_created(session, cmd.camera_id))
        self._emit_session_events(uow, session)
        uow.after_commit(lambda: self._occupancy.adjust(cmd.lot_id, +1))
        return ExitResult(status=ExitStatus.EXIT_WITHOUT_SESSION, session_id=session.id)

    # ------------------------------------------------------------ ПРЕДОПЛАТА / EXIT ПО ПЛАТЕЖУ
    async def prepay_quote(self, tenant_id: UUID, session_id: UUID, at: datetime) -> ExitQuote:
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            session = await repos.sessions.get(session_id)
            if session is None or session.status is not SessionStatus.ACTIVE:
                raise NotFoundError("active session not found")
            if await repos.sagas.active_for_session(session.id, for_update=False) is not None:
                raise ConflictError("session has an exit in progress: pay the pending obligation instead")
            lot = await repos.lots.get(session.lot_id)
            if lot is None:
                raise NotFoundError("lot not found")
            stored = await repos.vehicles.plate_encrypted(session.vehicle_id)
            decision = AccessDecision.normal()
            if stored is not None and self._access is not None:
                plate = self._crypto.decrypt(tenant_id, stored[0], stored[1])
                decision = await self._access.resolve(
                    tenant_id, session.lot_id, self._crypto.hash(tenant_id, plate), at
                )
            calc = await repos.tariff.calculate(session.lot_id, session.entry_time, at, _terms(decision))
            snapshot = calc.to_snapshot()
            snapshot["access"] = {"kind": decision.kind.value, "discount_percent": decision.discount_percent}
            return ExitQuote(
                session_id=session.id,
                lot_id=session.lot_id,
                entry_time=session.entry_time,
                at=at,
                amount_minor=calc.amount.minor,
                snapshot=snapshot,
                access_kind=decision.kind.value,
                source_id=decision.source_id,
                exit_grace_minutes=lot.policies.exit_grace_minutes,
            )

    async def close_inferred_by_payment(
        self, tenant_id: UUID, session_id: UUID, *, payment_id: UUID, paid_at: datetime
    ) -> bool:
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            session = await repos.sessions.get_for_update(session_id)
            if session is None or session.status is not SessionStatus.ACTIVE:
                return False
            if await repos.sagas.active_for_session(session.id, for_update=False) is not None:
                return False
            session.close(
                exit_time=paid_at,
                billing_minutes=ParkingSession.minutes_between(session.entry_time, paid_at),
                exit_kind=ExitKind.INFERRED_BY_PAYMENT,
                tariff_plan_version_id=session.tariff_plan_version_id,
                exit_camera_id=None,
                exit_event_id=None,
                reason="EXIT_INFERRED",
            )
            await repos.sessions.save(session)
            await repos.audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    action="SESSION_EXIT_INFERRED",
                    entity_type="parking_session",
                    entity_id=session.id,
                    lot_id=session.lot_id,
                    session_id=session.id,
                    reason="paid, no exit event within lot.exit_inferred_after_minutes (A-11)",
                    payload={"payment_id": str(payment_id), "paid_at": paid_at.isoformat()},
                )
            )
            self._emit_session_events(uow, session)
            uow.after_commit(self._decrement(session.lot_id))
            log.info("session_exit_inferred", session_id=str(session.id), payment_id=str(payment_id))
            return True

    async def sweep_prepaid(self, tenant_id: UUID, *, limit: int = 500, now: datetime | None = None) -> int:
        """Джоб: PAID-предоплаты, чьё окно выезда + exit_inferred_after_minutes истекло, закрывают сессию."""
        closed = 0
        now = now or self._clock.now()
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            candidates = await repos.payments.prepaid_expired(now, limit=limit)
            lots: dict[UUID, int] = {}
            due: list[tuple[UUID, UUID, datetime]] = []
            for ref in candidates:
                if ref.session_id is None or ref.paid_at is None:
                    continue
                session = await repos.sessions.get(ref.session_id)
                if session is None or session.status is not SessionStatus.ACTIVE:
                    continue
                if session.lot_id not in lots:
                    lot = await repos.lots.get(session.lot_id)
                    lots[session.lot_id] = lot.policies.exit_inferred_after_minutes if lot else 60
                if ref.paid_until is not None and now - ref.paid_until >= timedelta(minutes=lots[session.lot_id]):
                    due.append((session.id, ref.id, ref.paid_at))
        for session_id, payment_id, paid_at in due:
            if await self.close_inferred_by_payment(tenant_id, session_id, payment_id=payment_id, paid_at=paid_at):
                closed += 1
        return closed

    # ------------------------------------------------------------ SAGA RUNNER
    async def advance_saga(self, tenant_id: UUID, saga_id: UUID) -> SagaState:
        """Один шаг саги = одна транзакция.

        Идемпотентен: повторный вызов в том же состоянии не создаёт второго эффекта.
        """
        with span("exit.saga.step", tenant_id=tenant_id, saga_id=saga_id, **{FLOW_ATTR: "exit"}) as current:
            state = await self._advance_saga_traced(tenant_id, saga_id)
            current.set_attribute("saga.state", state.value)
            return state

    async def _advance_saga_traced(self, tenant_id: UUID, saga_id: UUID) -> SagaState:  # noqa: PLR0912 — таблица состояний саги
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            peek = await repos.sagas.get(saga_id)
            if peek is None:
                raise NotFoundError("saga not found")
            if peek.is_terminal:
                return peek.state
            # порядок блокировок везде один: сессия → сага (как в start_exit и ручных операциях оператора);
            # обратный порядок давал deadlock при одновременных close_manual и шаге саги (test_concurrency.py)
            await repos.sessions.get_for_update(peek.session_id)
            saga = await repos.sagas.get_for_update(saga_id)
            if saga is None or saga.is_terminal:
                return peek.state if saga is None else saga.state
            now = self._clock.now()
            state = saga.state
            if state is SagaState.ACCESS_RESOLVED:
                await self._step_access_resolved(uow, repos, saga, now)
            elif state is SagaState.PAYMENT_REQUIRED:
                await self._step_payment_required(uow, repos, saga, now)
            elif state is SagaState.PAYMENT_PENDING_VERIFICATION:
                assert saga.payment_id is not None
                payment = await repos.payments.status(saga.payment_id)
                if payment is not None and payment.status is PaymentStatus.PAID:  # status() или вебхук догнал
                    saga.settle(payment.id, now)
                    await self._step_open_barrier(uow, repos, saga, now)
                elif payment is not None and payment.status is PaymentStatus.FAILED:
                    await self._to_manual_review(
                        uow, repos, saga, ReviewReason.EXIT_PAYMENT_UNVERIFIED, now, "payment failed"
                    )
                elif saga.step_deadline is not None and saga.step_deadline <= now:
                    await self._to_manual_review(
                        uow, repos, saga, ReviewReason.EXIT_PAYMENT_UNVERIFIED, now, "payment status unknown"
                    )
            elif state is SagaState.PAYMENT_SETTLED:
                await self._step_open_barrier(uow, repos, saga, now)
            elif state is SagaState.BARRIER_OPENING:
                await self._step_barrier_opening(uow, repos, saga, now)
            elif state is SagaState.SESSION_CLOSED:
                await self._close_session(
                    uow, repos, saga, now, "CAMERA"
                )  # идемпотентно: сессия уже может быть закрыта
            elif state is SagaState.MANUAL_REVIEW:
                manual = saga.flags.get("manual_command_id")
                if manual and await repos.barriers.status(UUID(manual)) is CommandState.OK:
                    await self._close_session(uow, repos, saga, now, "MANUAL")
                elif (
                    saga.review_reason == ReviewReason.EXIT_BARRIER_FAILED.value
                    and saga.barrier_command_id is not None
                    and await repos.barriers.status(saga.barrier_command_id) is CommandState.OK
                ):
                    # команда опоздала к дедлайну шага (очередь), но шлагбаум открылся и машина уехала:
                    # закрываем как выезд по камере, оператору нечего делать (нагрузочный прогон фазы 8)
                    saga.barrier_result = "OK_LATE"
                    await self._close_session(uow, repos, saga, now, "CAMERA")
            await repos.sagas.save(saga)
            return saga.state

    async def _step_access_resolved(
        self, uow: UnitOfWork, repos: ParkingRepositories, saga: ExitSaga, now: datetime
    ) -> None:
        if saga.amount_due == 0:
            kind = saga.access_decision.get("kind", "NORMAL")
            method = {
                "VIP": PrivilegeMethod.VIP,
                "SUBSCRIPTION": PrivilegeMethod.SUBSCRIPTION,
                "WHITELIST": PrivilegeMethod.WHITELIST,
            }.get(kind, PrivilegeMethod.GRACE)
            if not saga.flags.get("prepaid"):
                # источник льготы: абонемент (source_id) либо само правило VIP/whitelist (rule_id) —
                # ck_privilege_source требует ссылку для любой нулевой льготной оплаты
                source = saga.access_decision.get("source_id") or saga.access_decision.get("rule_id")
                ref = await repos.payments.record_privilege(
                    session_id=saga.session_id,
                    lot_id=saga.lot_id,
                    saga_id=saga.id,
                    method=method,
                    source_id=UUID(source) if source else None,
                    snapshot=saga.calculation,
                    idempotency_key=_key("saga", saga.id, "settle"),
                )
                saga.settle(ref.id, now)
            else:
                saga.settle(UUID(str(saga.calculation["prepaid_payment_id"])), now)
            await self._step_open_barrier(uow, repos, saga, now)
            return
        ref = await repos.payments.create_obligation(
            session_id=saga.session_id,
            lot_id=saga.lot_id,
            saga_id=saga.id,
            amount_minor=saga.amount_due,
            snapshot=saga.calculation,
            idempotency_key=_key("saga", saga.id, "obligation"),
        )
        saga.require_payment(ref.id, now)

    async def _step_payment_required(
        self, uow: UnitOfWork, repos: ParkingRepositories, saga: ExitSaga, now: datetime
    ) -> None:
        assert saga.payment_id is not None
        payment = await repos.payments.status(saga.payment_id)
        if payment is not None and payment.status is PaymentStatus.PAID:
            saga.settle(payment.id, now)
            await self._step_open_barrier(uow, repos, saga, now)
        elif payment is not None and payment.status is PaymentStatus.FAILED:
            await self._to_manual_review(uow, repos, saga, ReviewReason.EXIT_PAYMENT_UNVERIFIED, now, "payment failed")
        elif payment is not None and payment.verification_pending and saga.state is SagaState.PAYMENT_REQUIRED:
            # провайдер не ответил, status() не знает: не ретраить create(), машину не выпускать, оператора уведомить
            saga.payment_unverified(now)
            uow.publish(
                IntegrationEvent(
                    type="parking.session.manual_review.v1",
                    tenant_id=saga.tenant_id,
                    aggregate_type="parking_session",
                    aggregate_id=saga.session_id,
                    payload={
                        "session_id": str(saga.session_id),
                        "lot_id": str(saga.lot_id),
                        "reason": "PAYMENT_PENDING_VERIFICATION",
                        "saga_id": str(saga.id),
                    },
                )
            )
            log.warning("saga_payment_unverified", saga_id=str(saga.id), payment_id=str(saga.payment_id))
        elif saga.saga_deadline is not None and saga.saga_deadline <= now:
            await repos.payments.cancel_obligation(saga.payment_id, "SAGA_EXPIRED")
            saga.expire(now)  # сессия остаётся ACTIVE: следующий EXIT начнёт новую сагу

    async def _step_open_barrier(
        self, uow: UnitOfWork, repos: ParkingRepositories, saga: ExitSaga, now: datetime
    ) -> None:
        if saga.skip_barrier:
            saga.open_barrier(None, now)
            saga.barrier_done("SKIPPED_LATE", now)
            await self._close_session(uow, repos, saga, now, "CAMERA")
            return
        command = await repos.barriers.request_open(
            lot_id=saga.lot_id,
            camera_id=saga.exit_camera_id,
            session_id=saga.session_id,
            saga_id=saga.id,
            source=CommandSource.AUTO_EXIT,
            idempotency_key=_key("saga", saga.id, "open"),
        )
        saga.open_barrier(command.command_id, now)
        if command.state is CommandState.SKIPPED:  # нет шлагбаума — нечего ждать
            saga.barrier_done("SKIPPED", now)
            await self._close_session(uow, repos, saga, now, "CAMERA")

    async def _step_barrier_opening(
        self, uow: UnitOfWork, repos: ParkingRepositories, saga: ExitSaga, now: datetime
    ) -> None:
        if saga.barrier_command_id is None:
            saga.barrier_done("SKIPPED", now)
            await self._close_session(uow, repos, saga, now, "CAMERA")
            return
        state = await repos.barriers.status(saga.barrier_command_id)
        if state is CommandState.OK:
            saga.barrier_done("OK", now)
            await self._close_session(uow, repos, saga, now, "CAMERA")
        elif state in (CommandState.FAILED, None) or (saga.step_deadline is not None and saga.step_deadline <= now):
            await self._to_manual_review(
                uow,
                repos,
                saga,
                ReviewReason.EXIT_BARRIER_FAILED,
                now,
                f"barrier command {state.value if state else 'missing'}",
            )

    async def _close_session(
        self, uow: UnitOfWork, repos: ParkingRepositories, saga: ExitSaga, now: datetime, how: str
    ) -> None:
        session = await repos.sessions.get_for_update(saga.session_id)
        if session is None:
            raise NotFoundError("session not found")
        if not session.is_terminal:
            if how == "CAMERA":  # автоматический выезд завершён (шлагбаум открыт/пропущен): SLI EXIT end-to-end
                metrics.exit_end_to_end_seconds.labels(paid="yes" if saga.amount_due > 0 else "no").observe(
                    max((now - saga.created_at).total_seconds(), 0.0)
                )
            version_raw = saga.calculation.get("tariff_plan_version_id")
            session.close(
                exit_time=saga.exit_event_time,
                billing_minutes=int(saga.calculation.get("billing_minutes", 0)),
                exit_kind=ExitKind.MANUAL if how == "MANUAL" else ExitKind.CAMERA,
                tariff_plan_version_id=UUID(version_raw) if version_raw else None,
                exit_camera_id=saga.exit_camera_id,
                exit_event_id=saga.exit_event_id,
                reason=None if how == "CAMERA" else "MANUAL_OPEN",
            )
            await repos.sessions.save(session)
            self._emit_session_events(uow, session)
            uow.after_commit(lambda: self._occupancy.adjust(session.lot_id, -1))
        if saga.state is not SagaState.SESSION_CLOSED:
            saga.barrier_done(saga.barrier_result or "OK", now)
        saga.complete(now)

    async def _to_manual_review(
        self,
        uow: UnitOfWork,
        repos: ParkingRepositories,
        saga: ExitSaga,
        reason: ReviewReason,
        now: datetime,
        error: str,
    ) -> None:
        saga.manual_review(reason.value, now, error)
        session = await repos.sessions.get_for_update(saga.session_id)
        if session is not None and not session.is_terminal and session.status is not SessionStatus.MANUAL_REVIEW:
            session.move_to_review(reason)
            await repos.sessions.save(session)
            self._emit_session_events(uow, session, saga_id=saga.id)
        log.warning("saga_manual_review", saga_id=str(saga.id), reason=reason.value, error=error)

    async def recover_due(self, scanner: SagaScanner, *, limit: int = 100) -> int:
        """Подбирает саги с просроченным шагом (после падения воркера) и продвигает их."""
        due = await scanner.due(self._clock.now(), limit=limit)
        for item in due:
            try:
                await self.advance_saga(item.tenant_id, item.saga_id)
            except Exception as exc:
                log.error("saga_recovery_failed", saga_id=str(item.saga_id), error_type=type(exc).__name__)
        return len(due)

    # ---------------------------------------------------------- MANUAL (I-15)
    async def correct_plate(
        self, actor: CurrentUser, session_id: UUID, new_plate: PlateNumber, *, reason: str
    ) -> SessionView:
        self._check_scope(actor, None)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id, for_update=True)
            plate_hash = self._crypto.hash(actor.tenant_id, new_plate)
            encrypted, version = self._crypto.encrypt(actor.tenant_id, new_plate)
            vehicle = await repos.vehicles.upsert(plate_hash, encrypted, version, self._clock.now())
            other = await repos.sessions.active_for_vehicle(session.lot_id, vehicle.id)
            if other is not None and other.id != session.id:
                raise ConflictError("another active session exists for this plate on the lot (I-1)")
            old = session.correct_plate(vehicle.id)
            await repos.sessions.save(session)
            correction_id = uuid4()
            metrics.manual_correction_total.labels(lot=str(session.lot_id)).inc()
            await repos.corrections.add(
                correction_id=correction_id,
                session=session,
                event_id=session.entry_event_id,
                old_vehicle_id=old,
                new_vehicle_id=vehicle.id,
                plate_encrypted=encrypted,
                enc_key_version=version,
                reason=reason,
                user_id=actor.user_id,
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="PLATE_CORRECTED",
                    entity_type="parking_session",
                    entity_id=session.id,
                    lot_id=session.lot_id,
                    session_id=session.id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={
                        "correction_id": str(correction_id),
                        "old_vehicle_id": str(old),
                        "new_vehicle_id": str(vehicle.id),
                    },
                )
            )
            for event in session.pull_events():
                if isinstance(event, PlateCorrected):
                    uow.publish(
                        IntegrationEvent(
                            type="parking.plate.corrected.v1",
                            tenant_id=actor.tenant_id,
                            aggregate_type="parking_session",
                            aggregate_id=session.id,
                            payload={
                                "session_id": str(session.id),
                                "lot_id": str(session.lot_id),
                                "old_vehicle_id": str(old),
                                "new_vehicle_id": str(vehicle.id),
                                "correction_id": str(correction_id),
                                "user_id": str(actor.user_id),
                            },
                        )
                    )
            full = await self._full_plates(repos, actor, session.lot_id)
            return SessionView.of(session, new_plate.value if full else new_plate.masked)

    async def confirm_entry(self, actor: CurrentUser, session_id: UUID, *, reason: str) -> SessionView:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id, for_update=True)
            session.confirm_entry()
            await repos.sessions.save(session)
            await repos.barriers.request_open(
                lot_id=session.lot_id,
                camera_id=session.entry_camera_id,
                session_id=session.id,
                saga_id=None,
                source=CommandSource.MANUAL,
                idempotency_key=_key("manual-entry", session.id, actor.user_id, self._clock.now().isoformat()),
                issued_by=actor.user_id,
                reason=reason,
            )
            await self._audit(repos, actor, "ENTRY_CONFIRMED_MANUAL", session, reason)
            uow.after_commit(lambda: self._occupancy.adjust(session.lot_id, +1))
            return SessionView.of(session)

    async def manual_open_exit(self, actor: CurrentUser, session_id: UUID, *, reason: str) -> SagaView:
        """«Открыть выезд».

        Выезд застрял на разборе (стрела не поднялась, оплата не подтверждена) — стрела открывается, платёж не трогаем
        (ADR-12). Выезд не начат — камера выезда не прочитала номер или прочитала с ошибкой («выезд без въезда»,
        MediaPark 2026-10-15: кнопка отвечала ошибкой) — начинается обычный выезд по тарифу: к оплате — кассир
        принимает деньги, и стрела откроется сама; бесплатно — открывается сразу. «Выезд без въезда» считается с
        момента, когда камера увидела машину (0 минут)."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id, for_update=True)
            saga = await repos.sagas.active_for_session(session.id, for_update=True)
            if saga is None:
                return await self._manual_exit(uow, repos, actor, session, reason)
            if saga.state is not SagaState.MANUAL_REVIEW:
                raise ConflictError(
                    "exit is in progress: take the payment, the barrier opens itself", code="EXIT_IN_PROGRESS"
                )
            command = await repos.barriers.request_open(
                lot_id=session.lot_id,
                camera_id=saga.exit_camera_id,
                session_id=session.id,
                saga_id=saga.id,
                source=CommandSource.MANUAL,
                idempotency_key=_key("manual-exit", saga.id, self._clock.now().isoformat()),
                issued_by=actor.user_id,
                reason=reason,
            )
            saga.flags["manual_command_id"] = str(command.command_id) if command.command_id else None
            saga.barrier_result = "MANUAL"
            if command.command_id is None:
                await self._close_session(uow, repos, saga, self._clock.now(), "MANUAL")
            await repos.sagas.save(saga)
            await self._audit(repos, actor, "EXIT_OPENED_MANUAL", session, reason)
            return SagaView.of(saga)

    async def _manual_exit(
        self, uow: UnitOfWork, repos: ParkingRepositories, actor: CurrentUser, session: ParkingSession, reason: str
    ) -> SagaView:
        unknown = session.review_reason is ReviewReason.EXIT_UNKNOWN_SESSION
        if session.status is not SessionStatus.ACTIVE and not unknown:
            raise ConflictError("session has no exit awaiting manual action")
        plate = await self._plate(repos, actor.tenant_id, session.vehicle_id)
        at = session.entry_time if unknown else self._clock.now()
        cmd = ExitCommand(
            tenant_id=actor.tenant_id,
            lot_id=session.lot_id,
            plate=plate,
            event_time=at,
            event_id=session.entry_event_id if unknown and session.entry_event_id else uuid4(),
            decision=await self._decide(actor.tenant_id, session.lot_id, plate, at),
            camera_id=session.entry_camera_id if unknown else None,
        )
        result = await self._exit_session(uow, repos, session, cmd, manual=True)
        saga = await repos.sagas.active_for_session(session.id, for_update=False)
        if saga is None or result.saga_id != saga.id:
            raise ConflictError("exit was not started: the session needs review", code="EXIT_NOT_STARTED")
        await self._audit(repos, actor, "EXIT_STARTED_MANUAL", session, reason, {"saga_id": str(saga.id)})
        return SagaView.of(saga)

    async def similar_sessions(
        self, actor: CurrentUser, session_id: UUID, *, limit: int = SIMILAR_LIMIT
    ) -> list[SimilarSession]:
        """Машины на площадке с номером, похожим на номер сессии («выезд без въезда»: камера выезда ошиблась) —
        лучшие первыми, при равенстве — въехавшие позже."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id)
            plate = await self._plate(repos, actor.tenant_id, session.vehicle_id)
            active = await repos.sessions.list_by_lot(
                session.lot_id, statuses=(SessionStatus.ACTIVE,), limit=SIMILAR_SCAN, after=None
            )
            others = [s for s in active if s.id != session.id and s.vehicle_id != session.vehicle_id]
            cards = await repos.vehicles.cards(list({s.vehicle_id for s in others}))
            found: list[tuple[float, ParkingSession, PlateNumber, VehicleAttributes]] = []
            for other in others:
                card = cards.get(other.vehicle_id)
                if card is None:
                    continue
                try:
                    number = self._crypto.decrypt(actor.tenant_id, card.plate_encrypted, card.enc_key_version)
                except Exception as exc:  # ключ номера недоступен — кандидат пропускается, разбор не падает
                    log.warning("similar_plate_skipped", session_id=str(other.id), error_type=type(exc).__name__)
                    continue
                distance = plate_distance(plate.value, number.value)
                if distance <= SIMILAR_MAX:
                    found.append((distance, other, number, card.attributes))
            found.sort(key=lambda f: (f[0], -f[1].entry_time.timestamp()))
            full = await self._full_plates(repos, actor, session.lot_id)
            return [
                SimilarSession(session=SessionView.of(s, n.value if full else n.masked, _attrs_view(attrs)), distance=d)
                for d, s, n, attrs in found[:limit]
            ]

    async def match_exit(self, actor: CurrentUser, session_id: UUID, target_id: UUID, *, reason: str) -> SagaView:
        """«Это она»: «выезд без въезда» — на самом деле машина target, которая стоит на площадке (номер на выезде
        прочитан с ошибкой). Разбор отменяется (CANCELLED, EXIT_MATCHED), у target начинается обычный выезд с момента,
        когда камера увидела машину: тариф, оплата у кассира, стрела — как если бы номер прочитан верно."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            review = await self._load_scoped(repos, actor, session_id, for_update=True)
            if review.status is not SessionStatus.MANUAL_REVIEW or (
                review.review_reason is not ReviewReason.EXIT_UNKNOWN_SESSION
            ):
                raise ConflictError("session is not an exit without entry", code="NOT_UNKNOWN_EXIT")
            target = await self._load_scoped(repos, actor, target_id, for_update=True)
            if target.lot_id != review.lot_id or target.status is not SessionStatus.ACTIVE:
                raise ConflictError("the matched car is not parked on this lot", code="MATCH_NOT_ACTIVE")
            plate = await self._plate(repos, actor.tenant_id, target.vehicle_id)
            cmd = ExitCommand(
                tenant_id=actor.tenant_id,
                lot_id=target.lot_id,
                plate=plate,
                event_time=max(review.entry_time, target.entry_time),
                event_id=review.entry_event_id or uuid4(),
                decision=await self._decide(actor.tenant_id, target.lot_id, plate, review.entry_time),
                camera_id=review.entry_camera_id,
            )
            occupied = review.occupies_space
            review.cancel(EXIT_MATCHED)
            await repos.sessions.save(review)
            await self._audit(
                repos, actor, "EXIT_MATCHED_MANUAL", review, reason, {"matched_session_id": str(target.id)}
            )
            if occupied:
                uow.after_commit(self._decrement(review.lot_id))
            result = await self._exit_session(uow, repos, target, cmd, manual=True)
            saga = await repos.sagas.active_for_session(target.id, for_update=False)
            if saga is None or result.saga_id != saga.id:
                raise ConflictError("exit was not started for the matched car", code="EXIT_NOT_STARTED")
            await self._audit(repos, actor, "EXIT_STARTED_MANUAL", target, reason, {"saga_id": str(saga.id)})
            return SagaView.of(saga)

    async def _plate(self, repos: ParkingRepositories, tenant_id: UUID, vehicle_id: UUID) -> PlateNumber:
        stored = await repos.vehicles.plate_encrypted(vehicle_id)
        if stored is None:
            raise NotFoundError("vehicle not found")
        return self._crypto.decrypt(tenant_id, stored[0], stored[1])

    async def _decide(self, tenant_id: UUID, lot_id: UUID, plate: PlateNumber, at: datetime) -> AccessDecision:
        if self._access is None:
            return AccessDecision.normal()
        return await self._access.resolve(tenant_id, lot_id, self._crypto.hash(tenant_id, plate), at)

    async def close_manual(self, actor: CurrentUser, session_id: UUID, *, reason: str) -> SessionView:
        """Стрелу подняли ключом / машина уехала: закрываем сессию вручную, живая сага завершается как BYPASSED."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id, for_update=True)
            now = self._clock.now()
            occupied = session.occupies_space
            saga = await repos.sagas.active_for_session(session.id, for_update=True)
            session.close(
                exit_time=now,
                billing_minutes=ParkingSession.minutes_between(session.entry_time, now),
                exit_kind=ExitKind.MANUAL,
                tariff_plan_version_id=None,
                reason=CLOSED_MANUAL,
            )
            await repos.sessions.save(session)
            if saga is not None:
                saga.barrier_result = "BYPASSED"
                if saga.state is not SagaState.SESSION_CLOSED:
                    saga.barrier_done("BYPASSED", now)
                saga.complete(now)
                await repos.sagas.save(saga)
            await self._audit(repos, actor, "SESSION_CLOSED_MANUAL", session, reason)
            self._emit_session_events(uow, session)
            if occupied:
                uow.after_commit(lambda: self._occupancy.adjust(session.lot_id, -1))
            return SessionView.of(session)

    async def mark_orphan(self, actor: CurrentUser, session_id: UUID, *, reason: str) -> SessionView:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id, for_update=True)
            occupied = session.occupies_space
            session.mark_orphan(ORPHANED_MANUAL)
            await repos.sessions.save(session)
            await self._audit(repos, actor, "SESSION_ORPHANED_MANUAL", session, reason)
            self._emit_session_events(uow, session)
            if occupied:
                uow.after_commit(lambda: self._occupancy.adjust(session.lot_id, -1))
            return SessionView.of(session)

    async def close_stale(
        self, actor: CurrentUser, lot_id: UUID, *, older_than_hours: int, reason: str | None, dry_run: bool
    ) -> StaleSessionsResult:
        """Машины уехали незамеченными (не было камеры выезда, камера не работала): незакрытые сессии площадки с въездом
        раньше N часов назад → ORPHAN, как у автоматической чистки по max_session_hours — без начисления, место
        освобождается. Сессии, по которым идёт выезд или оплата, не трогаются. dry_run — только посчитать (кассир видит,
        сколько закроет); иначе — с причиной, в аудите по каждой сессии (I-15)."""
        if not 1 <= older_than_hours <= 24 * 365:
            raise ValidationError("older_than_hours must be between 1 and 8760")
        if not dry_run and not (reason and reason.strip()):
            raise ValidationError("reason is required", code="REASON_REQUIRED")
        self._check_scope(actor, lot_id)
        cutoff = self._clock.now() - timedelta(hours=older_than_hours)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")
            stale = await repos.sessions.open_in_lot_before(lot_id, cutoff, limit=STALE_BATCH)
            if dry_run:
                return StaleSessionsResult(older_than=cutoff, found=len(stale), closed=0, skipped_in_exit=0)
            assert reason is not None
            closed = skipped = 0
            for candidate in stale:
                session = await repos.sessions.get_for_update(candidate.id)
                if session is None or session.is_terminal:
                    continue
                if await repos.sagas.active_for_session(session.id) is not None:
                    skipped += 1  # машина как раз выезжает — сессию закроет сага
                    continue
                session.mark_orphan(STALE_MANUAL)
                await repos.sessions.save(session)
                await self._audit(repos, actor, "SESSION_ORPHANED_MANUAL", session, reason.strip())
                self._emit_session_events(uow, session)
                closed += 1
        # счётчик занятости — по базе, а не вычитанием по одной: до чистки он мог уже разойтись с реальностью
        await self.reconcile_occupancy(actor.tenant_id, lot_id)
        log.info("stale_sessions_closed", lot_id=str(lot_id), closed=closed, skipped=skipped, hours=older_than_hours)
        return StaleSessionsResult(older_than=cutoff, found=len(stale), closed=closed, skipped_in_exit=skipped)

    async def sweep_stale(self, tenant_id: UUID, *, limit: int = 500) -> int:
        """Сессии старше lot.max_session_hours → ORPHAN (A-07). Вызывается джобом по каждому tenant."""
        closed = 0
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            now = self._clock.now()
            for session in await repos.sessions.stale_active(now - timedelta(hours=1), limit=limit):
                lot = await repos.lots.get(session.lot_id)
                if lot is None or now - session.entry_time < timedelta(hours=lot.policies.max_session_hours):
                    continue
                fresh = await repos.sessions.get_for_update(session.id)
                if fresh is None or fresh.is_terminal:
                    continue
                fresh.mark_orphan("MAX_SESSION_HOURS")
                await repos.sessions.save(fresh)
                self._emit_session_events(uow, fresh)
                uow.after_commit(self._decrement(fresh.lot_id))
                closed += 1
        return closed

    def _decrement(self, lot_id: UUID) -> Callable[[], Awaitable[None]]:
        async def hook() -> None:
            await self._occupancy.adjust(lot_id, -1)

        return hook

    # --------------------------------------------------------------- VEHICLES (v2)
    async def vehicle_info(self, actor: CurrentUser, plate: PlateNumber) -> VehicleView:
        """Карточка машины по номеру: марка/модель/цвет и открытая сессия на доступных площадках."""
        hashes = self._crypto.hashes_for_lookup(actor.tenant_id, plate)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            found = await repos.vehicles.attributes_by_hashes(hashes)
            sessions = await repos.sessions.list_by_plate_hashes(hashes, limit=5)
            full = await self._full_plates(repos, actor, None)
        allowed = actor.allowed_lots()
        active = next(
            (s for s in sessions if s.occupies_space and (allowed is None or s.lot_id in allowed)),
            None,
        )
        attrs = next(iter(found.values()), VehicleAttributes(make=None, model=None, color=None))
        return VehicleView(
            plate=plate.value if full else plate.masked,
            plate_format=plate.format.value,
            known=bool(found),
            attributes=_attrs_view(attrs),
            active_session_id=active.id if active else None,
        )

    async def set_vehicle_attributes(
        self,
        actor: CurrentUser,
        plate: PlateNumber,
        *,
        make: str | None,
        model: str | None,
        color: str | None,
        reason: str,
    ) -> VehicleView:
        """Марка/модель/цвет задаются оператором; машина создаётся, если номер ещё не встречался."""
        now = self._clock.now()
        plate_hash = self._crypto.hash(actor.tenant_id, plate)
        encrypted, version = self._crypto.encrypt(actor.tenant_id, plate)
        attrs = VehicleAttributes(make=_clean(make, 40), model=_clean(model, 60), color=_clean(color, 20))
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            vehicle = await repos.vehicles.upsert(plate_hash, encrypted, version, now)
            await repos.vehicles.set_attributes(
                vehicle.id, make=attrs.make, model=attrs.model, color=attrs.color, at=now
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="VEHICLE_UPDATED",
                    entity_type="vehicle",
                    entity_id=vehicle.id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"make": attrs.make, "model": attrs.model, "color": attrs.color},
                )
            )
            full = await self._full_plates(repos, actor, None)
        return VehicleView(
            plate=plate.value if full else plate.masked,
            plate_format=plate.format.value,
            known=True,
            attributes=_attrs_view(attrs),
        )

    async def set_session_vehicle_attributes(
        self,
        actor: CurrentUser,
        session_id: UUID,
        *,
        make: str | None,
        model: str | None,
        color: str | None,
        reason: str,
    ) -> SessionView:
        """Марка из карточки сессии: оператор видит машину на кадре и дописывает марку — номер ему не нужен
        (в интерфейсе он маскирован), машина берётся из сессии."""
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id)
            attrs = VehicleAttributes(make=_clean(make, 40), model=_clean(model, 60), color=_clean(color, 20))
            await repos.vehicles.set_attributes(
                session.vehicle_id, make=attrs.make, model=attrs.model, color=attrs.color, at=now
            )
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="VEHICLE_UPDATED",
                    entity_type="vehicle",
                    entity_id=session.vehicle_id,
                    lot_id=session.lot_id,
                    session_id=session.id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"make": attrs.make, "model": attrs.model, "color": attrs.color},
                )
            )
            return await self._view(repos, actor, session)

    async def vehicle_attributes_for(
        self, actor: CurrentUser, plates: list[PlateNumber]
    ) -> dict[str, VehicleAttributesView]:
        """Марки для списка номеров (правила доступа, абонементы) — один запрос по хешам."""
        if not plates:
            return {}
        by_hash = {self._crypto.hash(actor.tenant_id, p): p.value for p in plates}
        async with self._uow_factory(actor.tenant_id) as uow:
            found = await self._repos(uow).vehicles.attributes_by_hashes(list(by_hash))
        return {by_hash[h]: _attrs_view(a) for h, a in found.items() if h in by_hash}

    async def register_manual_entry(self, actor: CurrentUser, cmd: ManualEntryCommand, *, reason: str) -> SessionView:
        """Ручной въезд: камера не увидела машину (сбой, грязный номер), охрана впустила — оператор регистрирует
        сессию, чтобы стоянка посчиталась. Шлагбаум не открывается (машина уже внутри); чёрный список → разбор (I-8);
        причина обязательна и пишется в аудит (I-15)."""
        self._check_scope(actor, cmd.lot_id)
        plate = PlateNumber.parse(cmd.plate)
        now = self._clock.now()
        entry_time = cmd.entry_time or now
        if entry_time > now + timedelta(minutes=5):
            raise ValidationError("entry_time is in the future")
        if now - entry_time > timedelta(hours=72):
            raise ValidationError("entry_time is more than 72 hours ago")
        plate_hash = self._crypto.hash(actor.tenant_id, plate)
        encrypted, version = self._crypto.encrypt(actor.tenant_id, plate)
        decision_kind = AccessKind.NORMAL
        if self._access is not None:
            decision_kind = (await self._access.resolve(actor.tenant_id, cmd.lot_id, plate_hash, entry_time)).kind
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get(cmd.lot_id)
            if lot is None or not lot.active:
                raise NotFoundError("lot not found")
            vehicle = await repos.vehicles.upsert(plate_hash, encrypted, version, entry_time)
            if cmd.make or cmd.model or cmd.color:
                await repos.vehicles.set_attributes(
                    vehicle.id,
                    make=_clean(cmd.make, 40),
                    model=_clean(cmd.model, 60),
                    color=_clean(cmd.color, 20),
                    at=now,
                )
            blacklisted = decision_kind is AccessKind.BLACKLIST
            session = ParkingSession(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                lot_id=cmd.lot_id,
                vehicle_id=vehicle.id,
                entry_time=entry_time,
                status=SessionStatus.MANUAL_REVIEW if blacklisted else SessionStatus.ACTIVE,
                entry_access_kind=decision_kind.value,
                entry_manual=True,
                review_reason=ReviewReason.ENTRY_BLACKLIST if blacklisted else None,
            )
            try:
                await repos.sessions.add(session)
            except ConflictError as exc:
                raise ConflictError("this car is already on the lot (active session exists)") from exc
            if blacklisted:
                session.events.append(SessionMovedToReview(session_id=session.id, reason=ReviewReason.ENTRY_BLACKLIST))
            await repos.audit.record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="MANUAL_ENTRY",
                    entity_type="parking_session",
                    entity_id=session.id,
                    lot_id=session.lot_id,
                    session_id=session.id,
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"entry_time": entry_time.isoformat(), "access_kind": decision_kind.value},
                )
            )
            uow.publish(self._session_created(session, None))
            self._emit_session_events(uow, session)
            if session.occupies_space:
                uow.after_commit(lambda: self._occupancy.adjust(cmd.lot_id, +1))
            return await self._view(repos, actor, session)

    # ---------------------------------------------------------------- QUERIES
    async def get_session(self, actor: CurrentUser, session_id: UUID) -> SessionView:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            session = await self._load_scoped(repos, actor, session_id)
            return await self._view(repos, actor, session)

    async def list_sessions(
        self,
        actor: CurrentUser,
        lot_id: UUID,
        *,
        statuses: tuple[SessionStatus, ...] | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[SessionView]:
        self._check_scope(actor, lot_id)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")  # чужой tenant: существование площадки не подтверждается
            sessions = await repos.sessions.list_by_lot(lot_id, statuses=statuses, limit=limit, after=after)
            return [await self._view(repos, actor, s) for s in sessions]

    async def find_by_plate(self, actor: CurrentUser, plate: PlateNumber, *, limit: int = 20) -> list[SessionView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            hashes = self._crypto.hashes_for_lookup(actor.tenant_id, plate)
            allowed = actor.allowed_lots()
            sessions = await repos.sessions.list_by_plate_hashes(hashes, limit=limit)
            return [
                SessionView.of(s, plate.value if await self._full_plates(repos, actor, s.lot_id) else plate.masked)
                for s in sessions
                if allowed is None or s.lot_id in allowed
            ]

    async def review_queue(self, actor: CurrentUser, *, limit: int = 100) -> list[SessionView]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            sessions = await repos.sessions.review_queue(actor.allowed_lots(), limit=limit)
            return [await self._view(repos, actor, s) for s in sessions]

    async def get_saga_by_id(self, tenant_id: UUID, saga_id: UUID) -> SagaView:
        async with self._uow_factory(tenant_id) as uow:
            saga = await self._repos(uow).sagas.get(saga_id)
        if saga is None:
            raise NotFoundError("saga not found")
        return SagaView.of(saga)

    async def get_saga(self, actor: CurrentUser, session_id: UUID) -> SagaView | None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await self._load_scoped(repos, actor, session_id)
            saga = await repos.sagas.active_for_session(session_id)
            return None if saga is None else SagaView.of(saga)

    async def occupancy(self, actor: CurrentUser, lot_id: UUID) -> tuple[int, int]:
        """(занято, вместимость): Redis-проекция, при её отсутствии — точный COUNT."""
        self._check_scope(actor, lot_id)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            lot = await repos.lots.get(lot_id)
            if lot is None:
                raise NotFoundError("lot not found")
            cached = await self._occupancy.get(lot_id)
            occupied = cached if cached is not None else await repos.sessions.count_occupying(lot_id)
            return occupied, lot.capacity

    async def occupancy_by_entry_camera(self, actor: CurrentUser, lot_id: UUID) -> dict[UUID | None, int]:
        """v2 этап 5: точный COUNT по камерам въезда (None — ручной или выведенный въезд)."""
        self._check_scope(actor, lot_id)
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).sessions.count_occupying_by_entry_camera(lot_id)

    async def maintain_lots(self, scanner: SagaScanner) -> int:
        """Обслуживание раз в несколько минут: сессии старше max_session_hours → ORPHAN (по клиентам) и счётчик
        занятости каждой площадки = точное число машин в базе (Redis-проекция расходится при сбоях и чистках).
        Возвращает число закрытых сессий."""
        closed = 0
        tenants: set[UUID] = set()
        for tenant_id, lot_id in await scanner.active_lots():
            if tenant_id not in tenants:
                tenants.add(tenant_id)
                closed += await self.sweep_stale(tenant_id)
            await self.reconcile_occupancy(tenant_id, lot_id)
        return closed

    async def reconcile_occupancy(self, tenant_id: UUID, lot_id: UUID) -> int:
        async with self._uow_factory(tenant_id) as uow:
            exact = await self._repos(uow).sessions.count_occupying(lot_id)
        await self._occupancy.set(lot_id, exact)
        return exact

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _check_scope(actor: CurrentUser, lot_id: UUID | None) -> None:
        allowed = actor.allowed_lots()
        if lot_id is not None and allowed is not None and lot_id not in allowed:
            raise NotFoundError("lot not found")

    async def _load_scoped(
        self, repos: ParkingRepositories, actor: CurrentUser, session_id: UUID, *, for_update: bool = False
    ) -> ParkingSession:
        session = await (repos.sessions.get_for_update(session_id) if for_update else repos.sessions.get(session_id))
        if session is None:
            raise NotFoundError("session not found")
        self._check_scope(actor, session.lot_id)
        return session

    async def _view(self, repos: ParkingRepositories, actor: CurrentUser, session: ParkingSession) -> SessionView:
        """Сессия + номер (маской или целиком — политика площадки) + марка/модель/цвет одним запросом к vehicle."""
        card = await repos.vehicles.card(session.vehicle_id)
        if card is None:
            return SessionView.of(session)
        try:
            number = self._crypto.decrypt(actor.tenant_id, card.plate_encrypted, card.enc_key_version)
        except Exception:
            return SessionView.of(session, None, _attrs_view(card.attributes))
        full = await self._full_plates(repos, actor, session.lot_id)
        return SessionView.of(session, number.value if full else number.masked, _attrs_view(card.attributes))

    @staticmethod
    async def _full_plates(repos: ParkingRepositories, actor: CurrentUser, lot_id: UUID | None) -> bool:
        """Номер целиком: роль работает с машинами и площадка (без площадки — любая площадка клиента) это разрешает."""
        if actor.role not in FULL_PLATE_ROLES:
            return False
        if lot_id is None:
            return await repos.lots.shows_full_plates()
        lot = await repos.lots.get(lot_id)
        return lot is not None and lot.policies.show_full_plates

    async def _masked_plate(self, repos: ParkingRepositories, tenant_id: UUID, vehicle_id: UUID) -> str | None:
        stored = await repos.vehicles.plate_encrypted(vehicle_id)
        if stored is None:
            return None
        try:
            return self._crypto.decrypt(tenant_id, stored[0], stored[1]).masked
        except Exception:
            return None

    @staticmethod
    async def _audit(
        repos: ParkingRepositories,
        actor: CurrentUser,
        action: str,
        session: ParkingSession,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="parking_session",
                entity_id=session.id,
                lot_id=session.lot_id,
                session_id=session.id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload={"status": session.status.value, **(payload or {})},
            )
        )

    @staticmethod
    def _session_created(session: ParkingSession, camera_id: UUID | None) -> IntegrationEvent:
        return IntegrationEvent(
            type="parking.session.created.v1",
            tenant_id=session.tenant_id,
            aggregate_type="parking_session",
            aggregate_id=session.id,
            payload={
                "session_id": str(session.id),
                "lot_id": str(session.lot_id),
                "vehicle_id": str(session.vehicle_id),
                "entry_time": session.entry_time.isoformat(),
                "status": session.status.value,
                "entry_access_kind": session.entry_access_kind,
                **({"camera_id": str(camera_id)} if camera_id else {}),
            },
        )

    @staticmethod
    def _emit_session_events(uow: UnitOfWork, session: ParkingSession, *, saga_id: UUID | None = None) -> None:
        base = {
            "session_id": str(session.id),
            "lot_id": str(session.lot_id),
            "vehicle_id": str(session.vehicle_id),
            "entry_time": session.entry_time.isoformat(),
            "status": session.status.value,
        }
        for event in session.pull_events():
            if isinstance(event, SessionClosed):
                assert session.exit_time is not None
                uow.publish(
                    IntegrationEvent(
                        type="parking.session.closed.v1",
                        tenant_id=session.tenant_id,
                        aggregate_type="parking_session",
                        aggregate_id=session.id,
                        payload={
                            **base,
                            "exit_time": session.exit_time.isoformat(),
                            "billing_minutes": session.billing_minutes or 0,
                            "exit_kind": event.exit_kind.value,
                        },
                    )
                )
            elif isinstance(event, SessionOrphaned):
                uow.publish(
                    IntegrationEvent(
                        type="parking.session.orphaned.v1",
                        tenant_id=session.tenant_id,
                        aggregate_type="parking_session",
                        aggregate_id=session.id,
                        payload={**base, "reason": event.reason},
                    )
                )
            elif isinstance(event, SessionMovedToReview):
                uow.publish(
                    IntegrationEvent(
                        type="parking.session.manual_review.v1",
                        tenant_id=session.tenant_id,
                        aggregate_type="parking_session",
                        aggregate_id=session.id,
                        payload={**base, "reason": event.reason.value, "saga_id": str(saga_id) if saga_id else None},
                    )
                )


def _decision_json(decision: AccessDecision) -> dict[str, Any]:
    return {
        "kind": decision.kind.value,
        "discount_percent": decision.discount_percent,
        "source_id": str(decision.source_id) if decision.source_id else None,
        "rule_id": str(decision.rule_id) if decision.rule_id else None,
        "valid_until": decision.valid_until.isoformat() if decision.valid_until else None,
    }


def _clean(value: str | None, limit: int) -> str | None:
    """Пустая строка = «не указано»; обрезка пробелов и длины — ввод оператора, не доверяем форме."""
    if value is None:
        return None
    value = " ".join(value.split())[:limit]
    return value or None


def _attrs_view(a: VehicleAttributes) -> VehicleAttributesView:
    return VehicleAttributesView(make=a.make, model=a.model, color=a.color)
