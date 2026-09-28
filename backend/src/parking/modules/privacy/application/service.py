"""Процесс удаления ПДн (фаза 10 §3): каждый шаг — отдельная транзакция, удаление объектов MinIO — вне транзакции
(I-9). Открытый номер в сервис попадает только как PlateNumber и сразу превращается в хеш; в аудит и логи пишется
только id запроса и счётчики (I-10)."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.privacy.application.ports import DeletionRequestRepository, ObjectDeleter, SubjectDataRepository
from parking.modules.privacy.domain.request import DeletionRequest, DeletionState
from parking.platform.crypto import PlateCrypto
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import ConflictError, NotFoundError
from parking.shared.plate import PlateNumber

log = get_logger("privacy")
ANONYMIZED_PLATE = PlateNumber.parse("ANONYMIZED")  # заглушка, которую видят операторы вместо номера


@dataclass(frozen=True, slots=True)
class PrivacyRepositories:
    requests: DeletionRequestRepository
    subject: SubjectDataRepository
    audit: AuditWriter


@dataclass(frozen=True, slots=True)
class DeletionRequestView:
    id: UUID
    state: DeletionState
    legal_basis: str
    requested_at: datetime
    due_at: datetime
    verified_at: datetime | None
    retention_note: str | None
    anonymized_at: datetime | None
    media_deleted_at: datetime | None
    completed_at: datetime | None
    rejected_reason: str | None

    @classmethod
    def of(cls, r: DeletionRequest) -> DeletionRequestView:
        return cls(
            id=r.id,
            state=r.state,
            legal_basis=r.legal_basis,
            requested_at=r.requested_at,
            due_at=r.due_at,
            verified_at=r.verified_at,
            retention_note=r.retention_note,
            anonymized_at=r.anonymized_at,
            media_deleted_at=r.media_deleted_at,
            completed_at=r.completed_at,
            rejected_reason=r.rejected_reason,
        )


class PrivacyService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], PrivacyRepositories],
        crypto: PlateCrypto,
        objects: ObjectDeleter,
        clock: Clock,
        invalidate_cache: Callable[[UUID, bytes], object] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._crypto = crypto
        self._objects = objects
        self._clock = clock
        self._invalidate_cache = invalidate_cache

    async def request(
        self, actor: CurrentUser, plate: PlateNumber, *, legal_basis: str, contact: str | None, reason: str
    ) -> DeletionRequestView:
        contact_blob = self._crypto.encrypt_text(actor.tenant_id, contact) if contact else None
        req = DeletionRequest.open(
            tenant_id=actor.tenant_id,
            subject_plate_hash=self._crypto.hash(actor.tenant_id, plate),
            legal_basis=legal_basis,
            now=self._clock.now(),
            contact_encrypted=contact_blob,
            created_by=actor.user_id,
        )
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await repos.requests.add(req)
            await self._audit(repos, actor, "PRIVACY_REQUEST_OPENED", req, reason, {"legal_basis": legal_basis})
        return DeletionRequestView.of(req)

    async def get(self, actor: CurrentUser, request_id: UUID) -> DeletionRequestView:
        async with self._uow_factory(actor.tenant_id) as uow:
            req = await self._repos(uow).requests.get(request_id)
            if req is None:
                raise NotFoundError("deletion request not found")
            return DeletionRequestView.of(req)

    async def list(self, actor: CurrentUser, *, open_only: bool, limit: int) -> list[DeletionRequestView]:
        states = None
        if open_only:
            states = tuple(s for s in DeletionState if s not in (DeletionState.COMPLETED, DeletionState.REJECTED))
        async with self._uow_factory(actor.tenant_id) as uow:
            rows = await self._repos(uow).requests.list(states=states, limit=limit)
            return [DeletionRequestView.of(r) for r in rows]

    async def verify_identity(self, actor: CurrentUser, request_id: UUID, *, reason: str) -> DeletionRequestView:
        """Оператор подтвердил личность субъекта (документ на ТС): I-15 — reason и аудит обязательны."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            req.verify_identity(actor.user_id, self._clock.now())
            await repos.requests.save(req)
            await self._audit(repos, actor, "PRIVACY_IDENTITY_VERIFIED", req, reason)
            return DeletionRequestView.of(req)

    async def reject(self, actor: CurrentUser, request_id: UUID, *, reason: str) -> DeletionRequestView:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            req.reject(reason, self._clock.now())
            await repos.requests.save(req)
            await self._audit(repos, actor, "PRIVACY_REQUEST_REJECTED", req, reason)
            return DeletionRequestView.of(req)

    async def process(self, actor: CurrentUser, request_id: UUID) -> DeletionRequestView:
        """Проходит оставшиеся шаги после подтверждения личности. Каждый шаг идемпотентен и отдельно фиксируется."""
        while True:
            state = await self._current_state(actor.tenant_id, request_id)
            if state is DeletionState.IDENTITY_VERIFIED:
                await self._step_retention_check(actor, request_id)
            elif state is DeletionState.LEGAL_RETENTION_CHECK:
                await self._step_anonymize(actor, request_id)
            elif state is DeletionState.ANONYMIZATION:
                await self._step_delete_media(actor, request_id)
            elif state is DeletionState.MEDIA_DELETION:
                await self._step_audit(actor, request_id)
            elif state is DeletionState.AUDIT:
                await self._step_complete(actor, request_id)
            elif state is DeletionState.REQUESTED:
                raise ConflictError("identity must be verified before processing")
            else:
                return await self.get(actor, request_id)

    # ---------- шаги ----------

    async def _step_retention_check(self, actor: CurrentUser, request_id: UUID) -> None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            fp = await repos.subject.footprint([req.subject_plate_hash])
            if fp.active_sessions:
                raise ConflictError("subject has an active parking session; complete it before erasure")
            note = (
                f"vehicles={len(fp.vehicle_ids)} sessions={fp.sessions_total} payments={fp.payments_total} "
                f"media={len(fp.media_keys)}; финансовый ретеншен 7 лет: строки сессий/платежей сохраняются, "
                f"номер анонимизируется необратимым токеном (ЗРУ-547 ст. 20 / GDPR art. 17(3)(b))"
            )
            req.vehicle_ids = fp.vehicle_ids  # после анонимизации по хешу машины уже не найти — id фиксируются здесь
            req.retention_checked(note, self._clock.now())
            await repos.requests.save(req)

    async def _step_anonymize(self, actor: CurrentUser, request_id: UUID) -> None:
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            sentinel, version = self._crypto.encrypt(actor.tenant_id, ANONYMIZED_PLATE)
            result = await repos.subject.anonymize(
                req.vehicle_ids, token=secrets.token_bytes(32), sentinel_blob=sentinel, key_version=version, now=now
            )
            req.anonymized(now)
            await repos.requests.save(req)
            log.info(
                "privacy_anonymized",
                request_id=str(req.id),
                vehicles=result.vehicles,
                events=result.events,
                corrections=result.corrections,
            )
        if self._invalidate_cache is not None:  # кеш решений доступа по старому хешу
            await self._invalidate_cache(actor.tenant_id, req.subject_plate_hash)  # type: ignore[misc]

    async def _step_delete_media(self, actor: CurrentUser, request_id: UUID) -> None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            keys = await repos.subject.media_keys(req.vehicle_ids)
        deleted_versions = 0
        for key in keys:  # HTTP к MinIO — вне транзакции (I-9); все версии объекта
            deleted_versions += await self._objects.delete_all_versions(key)
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            await repos.subject.mark_media_deleted(keys, self._clock.now())
            req.media_deleted(self._clock.now())
            await repos.requests.save(req)
        log.info("privacy_media_deleted", request_id=str(request_id), objects=len(keys), versions=deleted_versions)

    async def _step_audit(self, actor: CurrentUser, request_id: UUID) -> None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            req.audited(self._clock.now())
            await repos.requests.save(req)
            await self._audit(
                repos,
                actor,
                "PRIVACY_ERASURE",
                req,
                "запрос субъекта ПДн выполнен",
                {"retention_note": req.retention_note},
            )

    async def _step_complete(self, actor: CurrentUser, request_id: UUID) -> None:
        """Завершение: сам запрос тоже перестаёт содержать хеш номера и контакт субъекта."""
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            req = await self._load(repos, request_id)
            req.subject_plate_hash = secrets.token_bytes(32)
            req.subject_contact_encrypted = None
            req.complete(self._clock.now())
            await repos.requests.save(req)

    # ---------- вспомогательное ----------

    async def _current_state(self, tenant_id: UUID, request_id: UUID) -> DeletionState:
        async with self._uow_factory(tenant_id) as uow:
            req = await self._repos(uow).requests.get(request_id)
            if req is None:
                raise NotFoundError("deletion request not found")
            return req.state

    async def _load(self, repos: PrivacyRepositories, request_id: UUID) -> DeletionRequest:
        req = await repos.requests.get(request_id, for_update=True)
        if req is None:
            raise NotFoundError("deletion request not found")
        return req

    async def _audit(
        self,
        repos: PrivacyRepositories,
        actor: CurrentUser,
        action: str,
        req: DeletionRequest,
        reason: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="deletion_request",
                entity_id=req.id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload={"state": req.state.value, **(payload or {})},
            )
        )
