"""Партнёры площадки (MediaPark, 2026-11-06): компьютерный клуб выпускает своих гостей. Гость говорит бармену номер
машины — бармен находит её на площадке и отмечает «за счёт клуба»; на выезде к оплате 0 сум и стрела открывается
сама. Клуб платит площадке фиксированную сумму в месяц (и, если задано, за каждую машину — цена на момент отметки).

Бармен (роль PARTNER) видит только машины, похожие на введённый номер, и свои отметки — не сессии площадки, не
деньги кассы. Отметку можно отменить, пока машина не выехала. Всё — с пользователем и в аудите (I-15)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from parking.modules.audit.application.contracts import ActorType, AuditEntry
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.parking.application.ports import Partner, PartnerMember, ValidationRow
from parking.modules.parking.application.services import ParkingRepositories, ParkingService, ReposFactory, UowFactory
from parking.modules.parking.domain.session import SessionStatus
from parking.modules.parking.domain.similar import plate_distance
from parking.platform.crypto import PlateCrypto
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from parking.shared.plate import InvalidPlateError, PlateNumber

log = get_logger("parking.partners")

FIND_SIMILAR_MAX = 2.0  # бармену — только очень похожие номера (опечатка, регион), не список всех машин
FIND_LIMIT = 3
FIND_SCAN = 3000


@dataclass(frozen=True, slots=True)
class PartnerCar:
    """Машина на площадке для бармена: номер, когда въехала, отмечена ли уже."""

    session_id: UUID
    plate: str
    entry_time: datetime
    validated: bool
    exact: bool


@dataclass(frozen=True, slots=True)
class ValidationView:
    id: UUID
    session_id: UUID
    plate: str | None
    entry_time: datetime
    exit_time: datetime | None
    state: str  # PARKED — машина ещё на площадке, LEFT — выехала, CANCELLED — отметка отменена
    price_per_car: int
    created_at: datetime
    created_by: str | None
    cancel_reason: str | None


@dataclass(frozen=True, slots=True)
class PartnerMonth:
    partner: Partner
    month: date
    count: int  # машин за месяц (без отменённых отметок)
    cars_total: int  # за машины (цена на момент отметки), тийины
    total: int  # к оплате партнёром: плата в месяц + за машины, тийины
    items: list[ValidationView]


class PartnerService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repos: ReposFactory,
        crypto: PlateCrypto,
        clock: Clock,
        parking: ParkingService,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._crypto = crypto
        self._clock = clock
        self._parking = parking

    # ------------------------------------------------------------------ бармен партнёра
    async def my_partner(self, actor: CurrentUser) -> Partner:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._partner_of(self._repos(uow), actor)

    async def find_cars(self, actor: CurrentUser, raw_plate: str) -> list[PartnerCar]:
        """Машины на площадке партнёра с этим номером; не нашлась точно — очень похожие (камера ошиблась в букве
        или регионе), не больше трёх. Списка всех машин бармен не получает."""
        try:
            plate = PlateNumber.parse(raw_plate)
        except InvalidPlateError:
            raise ValidationError("plate number is not valid", code="INVALID_PLATE") from None
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            partner = await self._partner_of(repos, actor)
            active = await repos.sessions.list_by_lot(
                partner.lot_id, statuses=(SessionStatus.ACTIVE,), limit=FIND_SCAN, after=None
            )
            cards = await repos.vehicles.cards(list({s.vehicle_id for s in active}))
            found: list[tuple[float, PartnerCar]] = []
            for session in active:
                card = cards.get(session.vehicle_id)
                if card is None:
                    continue
                try:
                    number = self._crypto.decrypt(actor.tenant_id, card.plate_encrypted, card.enc_key_version)
                except Exception as exc:  # ключ номера недоступен — машина пропускается, поиск не падает
                    log.warning("partner_find_skipped", session_id=str(session.id), error_type=type(exc).__name__)
                    continue
                distance = plate_distance(plate.value, number.value)
                if distance <= FIND_SIMILAR_MAX:
                    car = PartnerCar(
                        session_id=session.id,
                        plate=number.value,
                        entry_time=session.entry_time,
                        validated=session.partner_validation_id is not None,
                        exact=distance == 0,
                    )
                    found.append((distance, car))
        found.sort(key=lambda f: (f[0], -f[1].entry_time.timestamp()))
        return [car for _, car in found[:FIND_LIMIT]]

    async def validate(self, actor: CurrentUser, session_id: UUID) -> UUID:
        """«Выпустить за счёт клуба»: на выезде 0 сум и стрела сама; машина уже у выезда — открывается сразу."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            partner = await self._partner_of(repos, actor)
            session = await repos.sessions.get_for_update(session_id)
            if session is None or session.lot_id != partner.lot_id:
                raise NotFoundError("car not found on the lot")
            validation_id = uuid4()
            session.validate_by_partner(validation_id)
            await repos.sessions.save(session)
            await repos.partners.add_validation(
                validation_id=validation_id, partner=partner, session_id=session.id, created_by=actor.user_id
            )
            await self._parking.settle_exit_by_partner(uow, repos, session, validation_id)
            await self._audit(
                repos, actor, "PARTNER_VALIDATED", partner, session.id, f"выезд за счёт партнёра «{partner.name}»"
            )
        log.info("partner_validated", partner_id=str(partner.id), session_id=str(session_id))
        return validation_id

    async def cancel(self, actor: CurrentUser, validation_id: UUID, *, reason: str) -> None:
        """Отмена отметки (ошиблись машиной), пока машина не выехала: дальше она платит по тарифу."""
        if len(reason.strip()) < 3:
            raise ValidationError("reason is required", code="REASON_REQUIRED")
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            record = await repos.partners.get_validation(validation_id, for_update=True)
            if record is None:
                raise NotFoundError("validation not found")
            partner = await self._partner_for_record(repos, actor, record.partner_id)
            if record.cancelled_at is not None:
                raise ConflictError("validation is already cancelled", code="ALREADY_CANCELLED")
            session = await repos.sessions.get_for_update(record.session_id)
            exiting = session is not None and await repos.sagas.active_for_session(session.id) is not None
            if session is None or session.partner_validation_id != record.id or exiting:
                raise ConflictError("the car has already left or is leaving", code="ALREADY_LEFT")
            session.clear_partner_validation()
            await repos.sessions.save(session)
            await repos.partners.cancel_validation(record.id, by=actor.user_id, reason=reason, at=self._clock.now())
            await self._audit(repos, actor, "PARTNER_VALIDATION_CANCELLED", partner, session.id, reason)

    async def my_month(self, actor: CurrentUser, month: date) -> PartnerMonth:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            return await self._month(repos, actor, await self._partner_of(repos, actor), month)

    # ------------------------------------------------------------------ администратор площадки
    async def list_partners(self, actor: CurrentUser) -> list[Partner]:
        allowed = actor.allowed_lots()
        async with self._uow_factory(actor.tenant_id) as uow:
            partners = await self._repos(uow).partners.all()
        return [p for p in partners if allowed is None or p.lot_id in allowed]

    async def create_partner(
        self,
        actor: CurrentUser,
        *,
        lot_id: UUID,
        name: str,
        price_per_car: int,
        reason: str,
        monthly_fee: int = 0,
    ) -> Partner:
        _check(name, price_per_car, monthly_fee)
        self._check_lot(actor, lot_id)
        partner = Partner(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            lot_id=lot_id,
            name=name.strip(),
            price_per_car=price_per_car,
            monthly_fee=monthly_fee,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.lots.get(lot_id) is None:
                raise NotFoundError("lot not found")
            await repos.partners.add(partner)
            await self._audit(repos, actor, "PARTNER_CREATED", partner, None, reason)
        return partner

    async def update_partner(
        self,
        actor: CurrentUser,
        partner_id: UUID,
        *,
        name: str | None,
        price_per_car: int | None,
        active: bool | None,
        reason: str,
        monthly_fee: int | None = None,
    ) -> Partner:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            current = await self._scoped(repos, actor, partner_id, for_update=True)
            updated = Partner(
                id=current.id,
                tenant_id=current.tenant_id,
                lot_id=current.lot_id,
                name=(name or current.name).strip(),
                price_per_car=current.price_per_car if price_per_car is None else price_per_car,
                active=current.active if active is None else active,
                monthly_fee=current.monthly_fee if monthly_fee is None else monthly_fee,
            )
            _check(updated.name, updated.price_per_car, updated.monthly_fee)
            await repos.partners.save(updated)
            await self._audit(repos, actor, "PARTNER_UPDATED", updated, None, reason)
        return updated

    async def add_member(self, actor: CurrentUser, partner_id: UUID, user_id: UUID) -> None:
        """Вход бармена (роль PARTNER) — к партнёру; пользователь создан тем же запросом администратора."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            partner = await self._scoped(repos, actor, partner_id)
            await repos.partners.add_member(actor.tenant_id, partner.id, user_id)

    async def members(self, actor: CurrentUser, partner_id: UUID) -> list[PartnerMember]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await self._scoped(repos, actor, partner_id)
            return await repos.partners.members(partner_id)

    async def get_partner(self, actor: CurrentUser, partner_id: UUID) -> Partner:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._scoped(self._repos(uow), actor, partner_id)

    async def report(self, actor: CurrentUser, partner_id: UUID, month: date) -> PartnerMonth:
        """Расчёт с партнёром за месяц (по часам площадки): сколько машин отмечено, сколько он должен."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            return await self._month(repos, actor, await self._scoped(repos, actor, partner_id), month)

    # ------------------------------------------------------------------ общее
    async def _month(
        self, repos: ParkingRepositories, actor: CurrentUser, partner: Partner, month: date
    ) -> PartnerMonth:
        lot = await repos.lots.get(partner.lot_id)
        tz = ZoneInfo(lot.timezone if lot else "Asia/Tashkent")
        since = datetime(month.year, month.month, 1, tzinfo=tz)
        until = datetime(month.year + month.month // 12, month.month % 12 + 1, 1, tzinfo=tz)
        rows = await repos.partners.validations(partner.id, since=since, until=until)
        items = [self._view(actor, row) for row in rows]
        counted = [i for i in items if i.state != "CANCELLED"]
        cars_total = sum(i.price_per_car for i in counted)
        return PartnerMonth(
            partner=partner,
            month=date(month.year, month.month, 1),
            count=len(counted),
            cars_total=cars_total,
            total=partner.monthly_fee + cars_total,
            items=items,
        )

    def _view(self, actor: CurrentUser, row: ValidationRow) -> ValidationView:
        try:
            plate: str | None = self._crypto.decrypt(actor.tenant_id, row.plate_encrypted, row.enc_key_version).value
        except Exception:
            plate = None
        state = "CANCELLED" if row.cancelled_at else ("PARKED" if row.session_status == "ACTIVE" else "LEFT")
        return ValidationView(
            id=row.id,
            session_id=row.session_id,
            plate=plate,
            entry_time=row.entry_time,
            exit_time=row.exit_time,
            state=state,
            price_per_car=row.price_per_car,
            created_at=row.created_at,
            created_by=row.created_by,
            cancel_reason=row.cancel_reason,
        )

    @staticmethod
    async def _partner_of(repos: ParkingRepositories, actor: CurrentUser) -> Partner:
        partner = await repos.partners.of_user(actor.user_id)
        if partner is None or not partner.active:
            raise AuthorizationError("the user is not a member of an active partner", code="NOT_A_PARTNER")
        return partner

    async def _partner_for_record(self, repos: ParkingRepositories, actor: CurrentUser, partner_id: UUID) -> Partner:
        """Отменяет свою отметку бармен партнёра; администратор — любую на своих площадках."""
        if actor.has(Permission.PARTNERS_MANAGE):
            return await self._scoped(repos, actor, partner_id)
        partner = await self._partner_of(repos, actor)
        if partner.id != partner_id:
            raise NotFoundError("validation not found")
        return partner

    async def _scoped(
        self, repos: ParkingRepositories, actor: CurrentUser, partner_id: UUID, *, for_update: bool = False
    ) -> Partner:
        partner = await repos.partners.get(partner_id, for_update=for_update)
        if partner is None:
            raise NotFoundError("partner not found")
        self._check_lot(actor, partner.lot_id)
        return partner

    @staticmethod
    def _check_lot(actor: CurrentUser, lot_id: UUID) -> None:
        allowed = actor.allowed_lots()
        if allowed is not None and lot_id not in allowed:
            raise NotFoundError("lot not found")

    @staticmethod
    async def _audit(
        repos: ParkingRepositories,
        actor: CurrentUser,
        action: str,
        partner: Partner,
        session_id: UUID | None,
        reason: str,
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="partner",
                entity_id=partner.id,
                lot_id=partner.lot_id,
                session_id=session_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload={
                    "partner": partner.name,
                    "monthly_fee": partner.monthly_fee,
                    "price_per_car": partner.price_per_car,
                },
            )
        )


def _check(name: str, price_per_car: int, monthly_fee: int) -> None:
    if not 0 < len(name.strip()) <= 100:
        raise ValidationError("partner name is required")
    if price_per_car < 0 or monthly_fee < 0:
        raise ValidationError("price must be non-negative")
