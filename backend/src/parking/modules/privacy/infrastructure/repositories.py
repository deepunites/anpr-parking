from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.privacy.application.ports import AnonymizationResult, SubjectFootprint
from parking.modules.privacy.domain.request import DeletionRequest, DeletionState

_COLUMNS = (
    "id, tenant_id, state::text AS state, subject_plate_hash, subject_contact_encrypted, legal_basis, requested_at, "
    "due_at, verified_at, verified_by, retention_note, anonymized_at, media_deleted_at, audited_at, completed_at, "
    "rejected_reason, created_by, vehicle_ids"
)


def _load(row: Any) -> DeletionRequest:
    return DeletionRequest(
        id=row.id,
        tenant_id=row.tenant_id,
        state=DeletionState(row.state),
        subject_plate_hash=bytes(row.subject_plate_hash),
        subject_contact_encrypted=bytes(row.subject_contact_encrypted) if row.subject_contact_encrypted else None,
        legal_basis=row.legal_basis,
        requested_at=row.requested_at,
        due_at=row.due_at,
        verified_at=row.verified_at,
        verified_by=row.verified_by,
        retention_note=row.retention_note,
        anonymized_at=row.anonymized_at,
        media_deleted_at=row.media_deleted_at,
        audited_at=row.audited_at,
        completed_at=row.completed_at,
        rejected_reason=row.rejected_reason,
        created_by=row.created_by,
        vehicle_ids=list(row.vehicle_ids or []),
    )


class SqlDeletionRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, r: DeletionRequest) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO deletion_request (id, tenant_id, state, subject_plate_hash, subject_contact_encrypted,
                    legal_basis, requested_at, due_at, created_by, vehicle_ids)
                VALUES (:id, :tenant, CAST(:state AS deletion_state), :hash, :contact, :basis, :requested, :due, :by, :vehicles)
                """
            ),
            {
                "id": r.id,
                "tenant": r.tenant_id,
                "state": r.state.value,
                "hash": r.subject_plate_hash,
                "contact": r.subject_contact_encrypted,
                "basis": r.legal_basis,
                "requested": r.requested_at,
                "due": r.due_at,
                "by": r.created_by,
                "vehicles": r.vehicle_ids,
            },
        )

    async def get(self, request_id: UUID, *, for_update: bool = False) -> DeletionRequest | None:
        lock = " FOR UPDATE" if for_update else ""
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM deletion_request WHERE id = :id{lock}"), {"id": request_id}
            )
        ).first()
        return _load(row) if row else None

    async def save(self, r: DeletionRequest) -> None:
        await self._s.execute(
            text(
                """
                UPDATE deletion_request SET state = CAST(:state AS deletion_state), subject_plate_hash = :hash,
                    subject_contact_encrypted = :contact, verified_at = :verified_at, verified_by = :verified_by,
                    retention_note = :note, anonymized_at = :anonymized_at, media_deleted_at = :media_deleted_at,
                    audited_at = :audited_at, completed_at = :completed_at, rejected_reason = :rejected,
                    vehicle_ids = :vehicles
                WHERE id = :id
                """
            ),
            {
                "id": r.id,
                "state": r.state.value,
                "hash": r.subject_plate_hash,
                "contact": r.subject_contact_encrypted,
                "verified_at": r.verified_at,
                "verified_by": r.verified_by,
                "note": r.retention_note,
                "anonymized_at": r.anonymized_at,
                "media_deleted_at": r.media_deleted_at,
                "audited_at": r.audited_at,
                "completed_at": r.completed_at,
                "rejected": r.rejected_reason,
                "vehicles": r.vehicle_ids,
            },
        )

    async def list(self, *, states: tuple[DeletionState, ...] | None, limit: int) -> list[DeletionRequest]:
        params: dict[str, Any] = {"limit": limit}
        where = ""
        if states is not None:
            where = " WHERE state = ANY(CAST(:states AS deletion_state[]))"
            params["states"] = [s.value for s in states]
        rows = await self._s.execute(
            text(f"SELECT {_COLUMNS} FROM deletion_request{where} ORDER BY due_at, requested_at LIMIT :limit"),
            params,
        )
        return [_load(r) for r in rows]


class SqlSubjectDataRepository:
    """Данные субъекта по хешу номера. Анонимизация — через app.anonymize_vehicles (SECURITY DEFINER): у ролей
    приложения нет UPDATE на append-only anpr_event/manual_correction (I-11), функция сверяет tenant с контекстом."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def footprint(self, plate_hashes: list[bytes]) -> SubjectFootprint:
        vehicles = [
            r.id
            for r in await self._s.execute(
                text("SELECT id FROM vehicle WHERE plate_hash = ANY(:hashes) AND anonymized_at IS NULL"),
                {"hashes": plate_hashes},
            )
        ]
        if not vehicles:
            return SubjectFootprint(
                vehicle_ids=[], active_sessions=0, sessions_total=0, payments_total=0, media_keys=[]
            )
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT count(*) FILTER (WHERE s.status IN ('ACTIVE', 'MANUAL_REVIEW')) AS active,
                           count(*) AS sessions,
                           (SELECT count(*) FROM payment p WHERE p.session_id IN
                                (SELECT id FROM parking_session WHERE vehicle_id = ANY(:ids))) AS payments
                    FROM parking_session s WHERE s.vehicle_id = ANY(:ids)
                    """
                ),
                {"ids": vehicles},
            )
        ).one()
        return SubjectFootprint(
            vehicle_ids=vehicles,
            active_sessions=int(row.active),
            sessions_total=int(row.sessions),
            payments_total=int(row.payments),
            media_keys=await self.media_keys(vehicles),
        )

    async def media_keys(self, vehicle_ids: list[UUID]) -> list[str]:
        """Изображения событий субъекта: до и после анонимизации (события несут тот же хеш/токен, что и vehicle)."""
        if not vehicle_ids:
            return []
        rows = await self._s.execute(
            text(
                """
                SELECT DISTINCT e.image_object_key AS key
                FROM anpr_event e
                WHERE e.image_object_key IS NOT NULL
                  AND (e.plate_hash IN (SELECT plate_hash FROM vehicle WHERE id = ANY(:ids))
                       OR e.session_id IN (SELECT id FROM parking_session WHERE vehicle_id = ANY(:ids)))
                UNION
                SELECT m.object_key FROM media_object m
                WHERE m.deleted_at IS NULL AND m.session_id IN (SELECT id FROM parking_session WHERE vehicle_id = ANY(:ids))
                """
            ),
            {"ids": vehicle_ids},
        )
        return [str(r.key) for r in rows]

    async def anonymize(
        self, vehicle_ids: list[UUID], *, token: bytes, sentinel_blob: bytes, key_version: int, now: datetime
    ) -> AnonymizationResult:
        if not vehicle_ids:
            return AnonymizationResult(vehicles=0, events=0, corrections=0)
        tenant = (await self._s.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar_one()
        row = (
            await self._s.execute(
                text(
                    "SELECT * FROM app.anonymize_vehicles(CAST(:tenant AS uuid), :ids, :token, :sentinel, "
                    "CAST(:version AS smallint), :now)"
                ),
                {
                    "tenant": tenant,
                    "ids": vehicle_ids,
                    "token": token,
                    "sentinel": sentinel_blob,
                    "version": key_version,
                    "now": now,
                },
            )
        ).one()
        return AnonymizationResult(vehicles=int(row.vehicles), events=int(row.events), corrections=int(row.corrections))

    async def mark_media_deleted(self, keys: list[str], now: datetime) -> int:
        if not keys:
            return 0
        result = await self._s.execute(
            text(
                "UPDATE media_object SET deleted_at = :now, status = 'DELETED' WHERE object_key = ANY(:keys) AND deleted_at IS NULL"
            ),
            {"now": now, "keys": keys},
        )
        return int(getattr(result, "rowcount", 0) or 0)
